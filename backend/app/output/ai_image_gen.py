"""
AI image generation — removed.

FLUX.1-schnell and all diffusion model dependencies have been removed from this
platform. Decorative report assets are no longer generated.

Structure diagrams are now produced via LLM-generated Mermaid syntax rendered
by mermaid-cli (see output/mermaid_gen.py).
"""

raise ImportError(
    "ai_image_gen is no longer available. "
    "FLUX.1-schnell and diffusion dependencies have been removed. "
    "Use output/mermaid_gen.py for diagram generation."
)
