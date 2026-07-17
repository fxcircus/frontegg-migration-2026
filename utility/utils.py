
import os
from dotenv import load_dotenv
from utility.logger import log, log_success, log_error, log_warning, log_section, log_subsection, log_stats

# Load environment variables
load_dotenv()

# Constants from .env
BASE_URL_1 = os.getenv("BASE_URL_1")
BASE_URL_2 = os.getenv("BASE_URL_2")
CLIENT_ID_1 = os.getenv("CLIENT_ID_1")
API_KEY_1 = os.getenv("API_KEY_1")
CLIENT_ID_2 = os.getenv("CLIENT_ID_2")
API_KEY_2 = os.getenv("API_KEY_2")
