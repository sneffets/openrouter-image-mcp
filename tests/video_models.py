"""Trimmed copies of real `/videos/models` entries, shared by the video tests."""

VEO = {
    "id": "google/veo-3.1",
    "name": "Google: Veo 3.1",
    "supported_resolutions": ["720p", "1080p", "4K"],
    "supported_aspect_ratios": ["16:9", "9:16"],
    "supported_sizes": ["1280x720", "1920x1080"],
    "supported_durations": [4, 6, 8],
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": True,
    "pricing_skus": {
        "duration_seconds_with_audio": "0.40",
        "duration_seconds_with_audio_4k": "0.60",
        "duration_seconds_without_audio": "0.20",
    },
    "allowed_passthrough_parameters": ["personGeneration", "negativePrompt", "enhancePrompt"],
}
KLING = {
    "id": "kwaivgi/kling-v3.0-pro",
    "name": "Kling: Video v3.0 Pro",
    "supported_resolutions": ["720p"],
    "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
    "supported_durations": list(range(3, 16)),
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": False,
    "pricing_skus": {"duration_seconds": "0.112", "duration_seconds_with_audio": "0.168"},
    "allowed_passthrough_parameters": ["negative_prompt", "cfg_scale"],
}
SEEDANCE = {
    "id": "bytedance/seedance-2.5",
    "name": "ByteDance: Seedance 2.5",
    "supported_resolutions": ["480p", "720p"],
    "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
    "supported_durations": list(range(4, 31)),
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": True,
    "pricing_skus": {"video_tokens": "0.0000107"},
    "allowed_passthrough_parameters": ["watermark"],
}
WAN = {
    "id": "alibaba/wan-3.0",
    "name": "Alibaba: Wan 3.0",
    "supported_resolutions": ["480p", "720p", "1080p"],
    "supported_durations": list(range(2, 31)),
    "supported_frame_images": ["first_frame"],
    "generate_audio": True,
    "seed": True,
    "allowed_passthrough_parameters": [],
}
FLUX_EDIT = {
    "id": "black-forest-labs/flux-video-edit",
    "name": "Black Forest Labs: FLUX Video Edit",
    "generate_audio": False,
    "seed": False,
    "pricing_skus": {"cents_per_second_output": "3"},
    "allowed_passthrough_parameters": ["safety_tolerance"],
}
FLUX_UPSCALE = {
    "id": "black-forest-labs/flux-video-upscale",
    "name": "Black Forest Labs: FLUX Video Upscale",
    "upscale_factor": {"min": 1.5, "max": 3},
    "creativity": [0, 1],
    "generate_audio": False,
    "seed": False,
    "allowed_passthrough_parameters": ["safety_tolerance"],
}
VIDEO_MODELS = {"data": [VEO, KLING, SEEDANCE, WAN, FLUX_EDIT, FLUX_UPSCALE]}
