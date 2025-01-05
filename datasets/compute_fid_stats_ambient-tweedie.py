import os
from ambient_utils.eval_utils import calculate_inception_stats
import numpy as np

# Path to the directory containing FFHQ images
image_dir = "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/datasets/ffhq1024x1024/"
# stats_output_path = os.path.join(image_dir, "ffhq1024_stats.npz")
stats_output_path = "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/datasets/ffhq1024_stats.npz"
# Compute and save the custom statistics
def compute_custom_stats(image_dir, output_path):
    print(f"Computing custom statistics for images in: {image_dir}")

    try:
        # Calculate inception statistics
        mu, sigma, inception = calculate_inception_stats(
            image_path=image_dir, 
            num_expected=2048,  # Modify if you have a specific number of images
            seed=42, 
            max_batch_size=1, 
            distributed=False, 
            num_workers=1
        )

        # Save the statistics to a .npz file
        if not os.path.exists(os.path.dirname(output_path)):
            os.makedirs(os.path.dirname(output_path))

        np.savez(output_path, mu=mu, sigma=sigma, inception=inception)
        print(f"Custom statistics saved to: {output_path}")

    except Exception as e:
        print(f"Error computing custom statistics: {e}")

compute_custom_stats(image_dir, stats_output_path)
