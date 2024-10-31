import os
import pandas as pd
from glob import glob

# Path to your dataset
dataset_path = "./dataset/cat"

# Get all image files
image_files = glob(os.path.join(dataset_path, "*.jpg"))

# Create a DataFrame
df = pd.DataFrame({
    "image_path": image_files,
    "caption": ["A cat" for _ in image_files]  # You can customize captions if needed
})

# Save the DataFrame as a pickle file
df.to_pickle("./dataset/cat_dataset.pkl")

print(f"Dataset prepared and saved to ./dataset/cat_dataset.pkl")
print(f"Total images: {len(df)}")