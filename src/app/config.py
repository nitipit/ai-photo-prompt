"""Small runtime paths and the temporary visible-checkpoint seam."""

from pathlib import Path

APP_DIR = Path(__file__).parent
PROJECT_DIR = APP_DIR.parents[1]
TEMPLATE_DIR = APP_DIR / "templates"
DIST_DIR = PROJECT_DIR / "dist"
RUNTIME_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_DB_PATH = RUNTIME_DATA_DIR / "photo-prompt.shelfdb"
DEFAULT_CATALOG_PATH = DIST_DIR / "catalog.json"
DEFAULT_GENERATED_ROOT = RUNTIME_DATA_DIR / "generated"
DEFAULT_PI_WORKSPACE_ROOT = RUNTIME_DATA_DIR / "pi-rpc"
DEFAULT_PI_BRIDGE_PATH = Path.home() / ".pi" / "agent" / "extensions" / "codex-bridge.ts"
AI_PROVIDER_ENV = "PHOTO_PROMPT_AI_PROVIDER"
DEFAULT_AI_PROVIDER = "fake"
DEFAULT_PI_EXECUTABLE = "pi"
DEFAULT_PI_PROVIDER = "openai-codex"
DEFAULT_PI_MODEL = "gpt-5.6-luna"
DEFAULT_PI_IMAGE_THINKING = "minimal"
DEFAULT_PI_EVALUATOR_THINKING = "medium"
DEFAULT_PI_TIMEOUT_SECONDS = 240.0
DEFAULT_PI_MAX_OUTPUT_BYTES = 1024 * 1024

# Temporary until the round service owns real round creation and identifiers.
DEMO_ROUND_ID = "demo"
