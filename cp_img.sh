#!/bin/bash

# Define the source and destination directories
SOURCE_DIR="./datasets/laion-10k/"
DEST_DIR="./datasets/laion-10k-small/"

# Create the destination directory if it doesn't exist
mkdir -p "$DEST_DIR"

# Copy the first 100 images from the source to the destination directory
# Assuming files are sorted alphabetically/numerically
find "$SOURCE_DIR" -type f | sort | head -n 100 | while read -r file; do
    cp "$file" "$DEST_DIR"
done

echo "Copied the first 100 images to $DEST_DIR."

