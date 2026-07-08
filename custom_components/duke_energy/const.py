"""Constants for the Duke Energy integration."""

DOMAIN = "duke_energy"

# Auth0 OAuth2 configuration for Duke Energy
OAUTH2_AUTHORIZE = "https://login.duke-energy.com/authorize"
OAUTH2_TOKEN = "https://login.duke-energy.com/oauth/token"  # noqa: S105
OAUTH2_CLIENT_ID = "PitoKqxMh8thrFF8rRlYGrAs3LbSD2dj"

# Scopes required for Duke Energy API access
OAUTH2_SCOPES = ["openid", "profile", "email", "offline_access"]

# Auth0 client identifier (base64 encoded client info for mobile app)
AUTH0_CLIENT = "eyJuYW1lIjoiQXV0aDAuc3dpZnQiLCJlbnYiOnsiaU9TIjoiMjYuMiIsInN3aWZ0IjoiNi54In0sInZlcnNpb24iOiIyLjEzLjAifQ"  # noqa: E501

# Mobile app redirect URI - required by Duke Energy Auth0 config
MOBILE_REDIRECT_URI = "https://login.duke-energy.com/ios/com.duke-energy.app/callback"

# Options for cost statistics.
# The Duke Energy API does not expose per-interval cost, so cost is derived
# from consumption using a user-configured price source.
CONF_COST_MODE = "cost_mode"
COST_MODE_NONE = "none"
COST_MODE_FIXED = "fixed"
COST_MODE_ENTITY = "entity"
COST_MODES = [COST_MODE_NONE, COST_MODE_FIXED, COST_MODE_ENTITY]

CONF_FIXED_PRICE = "fixed_price"
CONF_PRICE_ENTITY = "price_entity"
CONF_MONTHLY_CHARGE = "monthly_charge"
