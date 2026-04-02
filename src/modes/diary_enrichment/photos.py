import io
from pathlib import Path

COLLAGE_MAX = 9
COLLAGE_SIZE = 1280


def find_day_photos(photos_dir: Path, date_str: str) -> list[Path]:
    if not photos_dir.exists():
        return []
    matches = list(photos_dir.glob(f"{date_str}*"))
    if not matches or not matches[0].is_dir():
        return []
    return sorted(
        f for f in matches[0].iterdir()
        if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
    )


def make_collage(paths: list[Path], size: int = COLLAGE_SIZE) -> io.BytesIO:
    """Build a 3x3 (or smaller) collage from photos. Returns JPEG in BytesIO."""
    from PIL import Image, ImageOps

    n = len(paths)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    cell = size // cols

    canvas = Image.new("RGB", (cols * cell, rows * cell), (0, 0, 0))
    for i, p in enumerate(paths):
        row, col = divmod(i, cols)
        with Image.open(str(p)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((cell, cell))
            x = col * cell + (cell - img.width) // 2
            y = row * cell + (cell - img.height) // 2
            canvas.paste(img, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=60)
    buf.seek(0)
    return buf
