import os
import shutil
from PIL import Image

def create_subset_with_resize(source_folder, target_folder, num_samples, output_size):
    """
    Create a subset of images and text files, resizing the images to the specified output size.

    Args:
        source_folder (str): Path to the source folder containing images and text files.
        target_folder (str): Path to the target folder to save the resized images and text files.
        num_samples (int): Number of samples to copy and resize.
        output_size (tuple): Target size for the images as (width, height).
    """
    # Ensure the target folder exists
    os.makedirs(target_folder, exist_ok=True)

    # Iterate over the first num_samples
    for i in range(num_samples):
        # Generate file names with zero-padded numbering
        img_file = f"{i:05d}.png"
        txt_file = f"{i:05d}.txt"

        # Define source paths
        img_src = os.path.join(source_folder, img_file)
        txt_src = os.path.join(source_folder, txt_file)

        # Define destination paths
        img_dst = os.path.join(target_folder, img_file)
        txt_dst = os.path.join(target_folder, txt_file)

        # Copy and resize the image if it exists
        if os.path.exists(img_src) and os.path.exists(txt_src):
            # Resize the image
            try:
                with Image.open(img_src) as img:
                    resized_img = img.resize(output_size, Image.LANCZOS)
                    resized_img.save(img_dst)
                print(f"Resized and copied: {img_file}")
            except Exception as e:
                print(f"Error processing image {img_file}: {e}")
                continue

            # Copy the text file
            shutil.copy(txt_src, txt_dst)
            print(f"Copied: {txt_file}")
        else:
            print(f"Missing: {img_file} or {txt_file}")

# Example usage
source_folder = "./ffhq1024x1024"
target_folder = "./ffhq_1024_mid"
num_samples = 2048
output_size = (1024, 1024)  # Resize images to 512x512

create_subset_with_resize(source_folder, target_folder, num_samples, output_size)
