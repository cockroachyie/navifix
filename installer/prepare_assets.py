"""
installer/prepare_assets.py
============================
Converts source images into the exact formats required by Inno Setup.
Run this ONCE on any machine (Mac, Linux, or Windows) with Pillow installed:

    pip install Pillow
    python installer/prepare_assets.py

Outputs (written to installer/assets/):
  icon.ico           — Multi-resolution Windows icon (16,32,48,64,128,256 px)
  wizard_banner.bmp  — 497 x 314 px installer left-side image (Inno Setup modern style)
  wizard_logo.bmp    — 55 x 55 px small logo (top-right of installer pages)
"""

import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
REPO_ROOT    = SCRIPT_DIR.parent
ASSETS_DIR   = SCRIPT_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

# Source images — use the generated ones or replace with your own
ICON_SRC    = SCRIPT_DIR / "assets" / "source_icon.jpg"    # generated NaviFix icon
BANNER_SRC  = SCRIPT_DIR / "assets" / "source_banner.jpg"  # generated wizard banner
LOGO_SRC    = REPO_ROOT / "logo.png"                        # existing project logo (fallback)


def make_ico(src: Path, dest: Path) -> None:
    """Create a multi-resolution .ico file from any source image."""
    print(f"  Creating {dest.name} from {src.name}…")
    img = Image.open(src).convert("RGBA")

    # Windows icon sizes (all required for good rendering at all DPI settings)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    resized = [img.resize(s, Image.LANCZOS) for s in sizes]

    resized[0].save(
        dest,
        format="ICO",
        sizes=sizes,
        append_images=resized[1:],
    )
    print(f"  ✓ {dest.name} — {len(sizes)} sizes embedded")


def make_bmp(src: Path, dest: Path, size: tuple[int, int]) -> None:
    """Resize and save as BMP (24-bit, no alpha — Inno Setup requirement)."""
    print(f"  Creating {dest.name} ({size[0]}x{size[1]}) from {src.name}…")
    img = Image.open(src).convert("RGB")
    img = img.resize(size, Image.LANCZOS)
    img.save(dest, format="BMP")
    print(f"  ✓ {dest.name}")


def main():
    print("")
    print("NaviFix — Asset Preparation")
    print("=" * 40)

    # ── 1. icon.ico ────────────────────────────────────────────────────────────
    ico_dest = ASSETS_DIR / "icon.ico"
    if ICON_SRC.exists():
        make_ico(ICON_SRC, ico_dest)
    elif LOGO_SRC.exists():
        print(f"  source_icon.jpg not found — using project logo.png as fallback")
        make_ico(LOGO_SRC, ico_dest)
    else:
        print("  WARNING: No icon source found. Using Inno Setup default icon.")
        # Create a simple placeholder so the build doesn't fail
        img = Image.new("RGBA", (256, 256), (11, 15, 26, 255))
        img.save(ico_dest, format="ICO", sizes=[(16, 16), (32, 32), (256, 256)])

    # ── 2. wizard_banner.bmp (497 × 314) ──────────────────────────────────────
    banner_dest = ASSETS_DIR / "wizard_banner.bmp"
    if BANNER_SRC.exists():
        make_bmp(BANNER_SRC, banner_dest, (497, 314))
    elif LOGO_SRC.exists():
        print(f"  source_banner.jpg not found — using project logo.png as fallback")
        make_bmp(LOGO_SRC, banner_dest, (497, 314))
    else:
        img = Image.new("RGB", (497, 314), (11, 15, 26))
        img.save(banner_dest, format="BMP")
        print(f"  ✓ wizard_banner.bmp (placeholder)")

    # ── 3. wizard_logo.bmp (55 × 55) ──────────────────────────────────────────
    logo_dest = ASSETS_DIR / "wizard_logo.bmp"
    src = ICON_SRC if ICON_SRC.exists() else LOGO_SRC
    if src.exists():
        make_bmp(src, logo_dest, (55, 55))
    else:
        img = Image.new("RGB", (55, 55), (11, 15, 26))
        img.save(logo_dest, format="BMP")
        print(f"  ✓ wizard_logo.bmp (placeholder)")

    print("")
    print("  Assets ready in installer/assets/")
    print("  You can now run: .\\installer\\build.ps1")
    print("")


if __name__ == "__main__":
    main()
