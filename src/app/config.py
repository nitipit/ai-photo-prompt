"""Small runtime paths and the temporary visible-checkpoint seam."""

from pathlib import Path

APP_DIR = Path(__file__).parent
PROJECT_DIR = APP_DIR.parents[1]
TEMPLATE_DIR = APP_DIR / "templates"
DIST_DIR = PROJECT_DIR / "dist"
RUNTIME_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_DB_PATH = RUNTIME_DATA_DIR / "photo-prompt.shelfdb"
DEFAULT_CATALOG_PATH = DIST_DIR / "catalog.json"

# Temporary until the round service owns real round creation and identifiers.
DEMO_ROUND_ID = "demo"
