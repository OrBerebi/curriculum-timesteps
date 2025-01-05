import os
import imageio
from PIL import Image, ImageDraw, ImageFont
import numpy as np

def create_gifs(validation_images_dir, output_gifs_dir):
    # Ensure output directory exists
    os.makedirs(output_gifs_dir, exist_ok=True)

    try:
        # Get all subdirectories sorted by numerical order
        subdirs = sorted(
            [d for d in os.listdir(validation_images_dir) if os.path.isdir(os.path.join(validation_images_dir, d))],
            key=lambda x: int(x)
        )
        print(f"Found subdirectories: {subdirs}")

        # Initialize a dictionary to store images for each frame
        frames = {i: [] for i in range(4)}

        for subdir in subdirs:
            subdir_path = os.path.join(validation_images_dir, subdir)
            images = sorted([img for img in os.listdir(subdir_path) if img.endswith('.png')])[:4]  # Limit to first 4 images
            print(f"Processing folder {subdir}: found images {images}")

            for i, image in enumerate(images):
                image_path = os.path.join(subdir_path, image)

                # Open image using Pillow to add text
                with Image.open(image_path) as img:
                    img = img.convert("RGB")  # Ensure image is in RGB mode

                    # Resize the image to reduce size (e.g., 512x512)
                    img = img.resize((128, 128), Image.Resampling.LANCZOS)

                    draw = ImageDraw.Draw(img)

                    # Load a font (adjust the path to a TTF file if needed)
                    try:
                        font = ImageFont.truetype("arial.ttf", 100)  # Adjusted font size for smaller images
                    except:
                        font = ImageFont.load_default()

                    # Add the step number (directory number) at the top left corner
                    step_number = f"Step {subdir}"
                    text_width, text_height = draw.textbbox((0, 0), step_number, font=font)[2:]  # Updated to use textbbox
                    draw.rectangle((20, 20, 20 + text_width, 20 + text_height), fill="black")  # Add background for visibility
                    draw.text((20, 20), step_number, fill="white", font=font)  # Draw the text

                    # Convert back to numpy array for imageio
                    frames[i].append(np.array(img))

        # Create GIFs for each frame
        for i in range(4):
            output_gif_path = os.path.join(output_gifs_dir, f"{i:06d}.gif")

            # Optimize GIF size by reducing quality and using fewer colors
            imageio.mimsave(output_gif_path, frames[i], duration=0.5, quantize=256, palettesize=64)
            print(f"Saved GIF: {output_gif_path}")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    validation_images_dir = "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/ffhq_base_val/validation_images/"
    output_gifs_dir = "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/ffhq_base_val/validation_gifs/"

    print("Starting GIF creation process...")
    create_gifs(validation_images_dir, output_gifs_dir)
    print("GIF creation process completed.")
