import argparse
import numpy as np
import random
import torch
import torch.amp
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_
from torch.cuda.amp import GradScaler, autocast
import torchvision.transforms as transforms
from datasets import load_dataset
from diffusers.optimization import get_scheduler
from tqdm.auto import tqdm
from torchinfo import summary

from simple_diffusion.scheduler import DDIMScheduler
from simple_diffusion.model import UNet
from simple_diffusion.utils import save_images
from simple_diffusion.dataset import CustomDataset, get_dataset
import pandas as pd
import webdataset as wds

from simple_diffusion.ema import EMA

SEED = 42

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)

n_timesteps = 1024
n_inference_timesteps = 256

def _grayscale_to_rgb(img):
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def main(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = UNet(3, image_size=args.resolution, hidden_dims=[64, 128, 256, 512], use_flash_attn=args.use_flash_attn)
    noise_scheduler = DDIMScheduler(num_train_timesteps=n_timesteps,
                                    beta_schedule="cosine")
    model = model.to(device)

    if args.pretrained_model_path:
        pretrained = torch.load(args.pretrained_model_path)["model_state"]
        model.load_state_dict(pretrained)

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
    )

    tfms = transforms.Compose([
        transforms.Resize((args.resolution, args.resolution)),
        transforms.Lambda(_grayscale_to_rgb),
        transforms.ToTensor(),
        # normalize to [-1, 1] for faster convergence and numerical stability
        transforms.Lambda(lambda x: x * 2 - 1)
    ])

    if args.dataset_name in ["combined", "yfcc7m"]:
        dataset = get_dataset(args.dataset_name,
                              args.dataset_path,
                              transforms=tfms)
    elif args.dataset_name is not None:
        dataset = load_dataset(
            args.dataset_name,
            args.dataset_config_name,
            cache_dir=args.cache_dir,
            split="train",
        )

        def aug(examples):
            images = [
                tfms(image) for image in examples["image"]
            ]
            return {"image": images}

        dataset.set_transform(aug)
    else:
        def aug(examples):
            images = [
                tfms(image) for image in examples
            ]
            return {"image": images}

        df = pd.read_pickle(args.dataset_path)
        dataset = CustomDataset(df, tfms)

    if args.dataset_name == "yfcc7m":
        train_dataloader = wds.WebLoader(dataset,
                                         num_workers=2,
                                         batch_size=args.train_batch_size)
        # hardcoded for num images = 7329280 :/
        steps_per_epcoch = 7329280 // args.train_batch_size
    else:
        train_dataloader = torch.utils.data.DataLoader(
            dataset, batch_size=args.train_batch_size, shuffle=True)
        steps_per_epcoch = len(train_dataloader)

    total_num_steps = (steps_per_epcoch * args.num_epochs) // args.gradient_accumulation_steps
    total_num_steps += int(total_num_steps * 10/100)
    gamma = args.gamma
    ema = EMA(model, gamma, total_num_steps)

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps,
        num_training_steps=total_num_steps,
    )

    summary(model, [(1, 3, args.resolution, args.resolution), (1,)], verbose=1)

    scaler = GradScaler(enabled=args.fp16_precision)
    global_step = 0
    losses = []
    for epoch in range(args.num_epochs):
        progress_bar = tqdm(total=steps_per_epcoch)
        progress_bar.set_description(f"Epoch {epoch}")
        losses_log = 0
        for step, batch in enumerate(train_dataloader):
            orig_images = batch["image"].to(device)

            batch_size = orig_images.shape[0]
            noise = torch.randn(orig_images.shape).to(device)
            timesteps = torch.randint(0,
                                      noise_scheduler.num_train_timesteps,
                                      (batch_size,),
                                      device=device).long()
            noisy_images = noise_scheduler.add_noise(orig_images, noise,
                                                     timesteps)

            optimizer.zero_grad()
            with autocast(enabled=args.fp16_precision):
                noise_pred = model(noisy_images, timesteps)["sample"]
                loss = F.l1_loss(noise_pred, noise)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            ema.update_params(gamma)
            gamma = ema.update_gamma(global_step)

            if args.use_clip_grad:
                clip_grad_norm_(model.parameters(), 1.0)

            lr_scheduler.step()

            progress_bar.update(1)
            losses_log += loss.detach().item()
            logs = {
                "loss_avg": losses_log / (step + 1),
                "loss": loss.detach().item(),
                "lr": lr_scheduler.get_last_lr()[0],
                "step": global_step,
                "gamma": gamma
            }

            progress_bar.set_postfix(**logs)
            global_step += 1

            # Generate sample images for visual inspection
            if global_step % args.save_model_steps == 0:
                ema.ema_model.eval()
                with torch.no_grad():
                    # has to be instantiated every time, because of reproducibility
                    generator = torch.manual_seed(0)
                    generated_images = noise_scheduler.generate(
                        ema.ema_model,
                        num_inference_steps=n_inference_timesteps,
                        generator=generator,
                        eta=1.0,
                        batch_size=args.eval_batch_size)

                    save_images(generated_images, epoch, args)

                    torch.save(
                        {
                            'model_state': model.state_dict(),
                            'ema_model_state': ema.ema_model.state_dict(),
                            'optimizer_state': optimizer.state_dict(),
                        }, args.output_dir)

        progress_bar.close()
        losses.append(losses_log / (step + 1))

# python train.py   --dataset_path="dataset/cat_dataset.pkl"   --resolution=64   --output_dir="trained_models/ddpm-ema-64.pth"   --train_batch_size=16   --num_epochs=121 --gradient_accumulation_steps=1   --learning_rate=1e-4   --lr_warmup_steps=300  --save_model_steps=250

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simple example of a training script.")
    parser.add_argument("--dataset_name", type=str, default=None)
    parser.add_argument('--dataset_path',
                        type=str,
                        default='./data',
                        help='Path where datasets will be saved')
    parser.add_argument("--dataset_config_name", type=str, default=None)
    parser.add_argument("--output_dir",
                        type=str,
                        default="trained_models/ddpm-model-64.pth")
    parser.add_argument("--samples_dir", type=str, default="test_samples/")
    parser.add_argument("--loss_logs_dir", type=str, default="training_logs")
    parser.add_argument("--cache_dir", type=str, default=None)
    parser.add_argument("--resolution", type=int, default=64)
    parser.add_argument("--train_batch_size", type=int, default=16)
    parser.add_argument("--eval_batch_size", type=int, default=16)
    parser.add_argument("--num_epochs", type=int, default=1)
    parser.add_argument("--save_model_steps", type=int, default=1000)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument("--learning_rate", type=float, default=1e-4)
    parser.add_argument("--lr_scheduler", type=str, default="cosine")
    parser.add_argument("--lr_warmup_steps", type=int, default=100)
    parser.add_argument("--adam_beta1", type=float, default=0.9)
    parser.add_argument("--adam_beta2", type=float, default=0.99)
    parser.add_argument("--adam_weight_decay", type=float, default=0.0)
    parser.add_argument("--use_clip_grad", action='store_true')
    parser.add_argument('--use_flash_attn', action='store_true')
    parser.add_argument("--logging_dir", type=str, default="logs")
    parser.add_argument("--pretrained_model_path",
                        type=str,
                        default=None,
                        help="Path to pretrained model")
    parser.add_argument('--fp16_precision',
                        action='store_true',
                        help='Whether to use 16-bit precision for GPU training')
    parser.add_argument('--gamma',
                    default=0.996,
                    type=float,
                    help='Initial EMA coefficient')

    args = parser.parse_args()

    if args.dataset_name is None and args.dataset_path is None:
        raise ValueError(
            "You must specify either a dataset name from the hub or a train data directory."
        )

    main(args)
