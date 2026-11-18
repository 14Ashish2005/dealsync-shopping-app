from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from backend.visual_search.analyzer import VisionAnalyzer


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
_MODEL_NAME = "qwen/qwen3.8-27b"
_VISION_TIMEOUT_SECONDS = 30


def create_vision_analyzer() -> VisionAnalyzer:
    load_dotenv(dotenv_path=ENV_PATH)
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        raise ValueError(
            "GROQ_API_KEY is missing. Please set your GROQ_API_KEY in the .env file inside the project directory."
        )

    from langchain_groq import ChatGroq

    model = ChatGroq(
        model=_MODEL_NAME,
        temperature=0,
        timeout=_VISION_TIMEOUT_SECONDS,
    )
    return VisionAnalyzer(model)
