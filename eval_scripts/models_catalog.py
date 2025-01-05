"""Registered models that are trained locally."""
models = {
    "sdxl": {
        "dataset_size": "full",  # how many examples were in the dataset. Use either full or number.
        "consistency": False,  # whether this model was trained with consistency.
        "trained_with_lora": False,
        "timestep_nature": 0,  # level of noise that was present in the training examples. Number should be between 0 and 1000 for SDXL.
        "ckpt_path": None,  # add local checkpoint path
        "desc": "SDXL vanilla model.",
    },
    "sd_dumb": {
        "dataset_size": 100,  # how many examples were in the dataset. Use either full or number.
        "consistency": True,  # whether this model was trained with consistency.
        "trained_with_lora": True,
        "timestep_nature": 50,  # level of noise that was present in the training examples. Number should be between 0 and 1000 for SDXL.
        "ckpt_path": "/gpfs0/bgu-br/users/berebio/GitHub/ambient-tweedie/laion_low_level/checkpoint-49800/",  # add local checkpoint path
        "desc": "SDXL dumb model.",
    },
}
