from cleanfid import fid
import os

# Path to the directory containing FFHQ images
image_dir = "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/datasets/ffhq1024x1024/"
stats_output_path = os.path.join(image_dir, "ffhq1024_stats.npz")

# Compute and save the FID statistics
def compute_fid_stats(image_dir, output_path):
    print(f"Computing FID statistics for images in: {image_dir}")
    fid.make_custom_stats(name="ffhq1024", fdir=image_dir, num=None)
    stats_src_path = fid.get_folder("ffhq1024")

    # Move the stats file to the desired output path
    stats_file = os.path.join(stats_src_path, "fid_stats_ffhq1024.npz")
    if os.path.exists(stats_file):
        os.rename(stats_file, output_path)
        print(f"FID statistics saved to: {output_path}")
    else:
        print("Error: FID statistics file was not created.")

compute_fid_stats(image_dir, stats_output_path)

