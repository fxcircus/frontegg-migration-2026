import requests
from datetime import datetime, timedelta
from utility.logger import get_logger, log_success, log_error, log_warning

class FronteggClient:
    def __init__(self, base_url, client_id, secret):
        self.base_url = base_url
        self.client_id = client_id
        self.secret = secret
        self.session = requests.Session()
        self.token = None
        self.token_expiry = None
        self.logger = get_logger()
        self.authenticate()  # Authenticate upon initialization

    def authenticate(self):
        """Authenticate using client ID and secret, retrieving a token."""
        self.logger.info(f"🔐 Authenticating with Frontegg (Client: {self.client_id[:8]}...)")
        endpoint = self.base_url + '/auth/vendor'
        req_body = {
            'clientId': self.client_id,
            'secret': self.secret
        }
        try:
            response = self.session.post(endpoint, json=req_body)
            response.raise_for_status()
            response_json = response.json()
            token = response_json.get("token")
            expires_in = response_json.get("expiresIn", 3600)
            if not token:
                raise ValueError("Authentication failed: No token found in response.")
            self.token = token
            self.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
            log_success(f"Authentication successful for {self.base_url}")
            self.logger.debug(f"Token expires at: {self.token_expiry}")
        except requests.exceptions.RequestException as e:
            log_error(f"Authentication failed: {e}")
            if hasattr(e, 'response') and e.response:
                self.logger.debug(f"Response: {e.response.text}")
            raise
        except ValueError as e:
            log_error(f"Authentication error: {str(e)}")
            raise

    def request(self, method, endpoint, data=None):
        """Make an API request, refreshing the token if needed."""
        if not self.token or datetime.utcnow() >= self.token_expiry:
            log_warning("Token expired or missing, re-authenticating...")
            self.authenticate()

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        self.logger.debug(f"API Request: {method} {url}")
        try:
            response = self.session.request(method, url, headers=headers, json=data)
            response.raise_for_status()
            self.logger.debug(f"Response: {response.status_code}")
            return response.json()
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"Request failed: {method} {url} - Status: {e.response.status_code}")
            raise
