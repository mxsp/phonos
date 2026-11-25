import os
import sys
# Add the directory containing 'production' to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "production"))

from production.database import db_connection
from production.config import Config # Import Config to access DATABASE_URL

# Configure logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def update_user_credits(target_user_id: str, new_credits: int):
    """
    Updates the credits for a specific user.

    Args:
        target_user_id (str): The user_id (UUID) of the user to update.
        new_credits (int): The new credit amount to set for the user.
    """
    if not target_user_id:
        logger.error("User ID cannot be empty.")
        return

    logger.info(f"Attempting to update credits for user_id: {target_user_id} to {new_credits}")

    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET credits = %s WHERE user_id = %s",
                (new_credits, target_user_id)
            )
            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Successfully updated credits for user_id: {target_user_id} to {new_credits}.")
            else:
                logger.warning(f"No user found with user_id: {target_user_id}. No credits updated.")
    except Exception as e:
        logger.error(f"Error updating user credits: {e}")

if __name__ == "__main__":
    # This script should be run within the virtual environment
    # Example usage: python update_user_credits.py <user_uuid>
    
    # You need to provide the user_id (UUID string) as a command-line argument.
    # Replace with the actual user_id you want to target.
    # For example: "a1b2c3d4-e5f6-7890-1234-567890abcdef"
    
    # Placeholder for the user_id
    user_id_to_update = os.getenv("TARGET_USER_UUID") # Using environment variable for safer passing
    
    if len(sys.argv) > 1:
        user_id_to_update = sys.argv[1]

    if not user_id_to_update:
        logger.error("Please provide the user_id (UUID string) as a command-line argument or via TARGET_USER_UUID environment variable.")
        sys.exit(1)

    # Set the desired credit amount
    CREDIT_AMOUNT = 10000
    update_user_credits(target_user_id=user_id_to_update, new_credits=CREDIT_AMOUNT)
