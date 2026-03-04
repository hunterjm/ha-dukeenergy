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
MOBILE_REDIRECT_URI = (
    "https://login.duke-energy.com/ios/"
    "com.duke-energy.app/callback"
)
