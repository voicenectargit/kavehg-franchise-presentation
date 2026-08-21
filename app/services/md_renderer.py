"""
Markdown → HTML renderer (server-side)
"""
import markdown


_MD = markdown.Markdown(
    extensions=[
        "tables",
        "fenced_code",
        "codehilite",
        "toc",
        "nl2br",
        "sane_lists",
        "pymdownx.tasklist",
    ],
    extension_configs={
        "codehilite": {"css_class": "highlight", "guess_lang": True},
        "toc": {"permalink": True},
    },
)


def render_markdown(md_text: str) -> str:
    _MD.reset()
    return _MD.convert(md_text)
