import logging
import requests
import re
import datetime

from sqlalchemy import or_
from app.extensions import db
from app.models import User, Invitation, Library
from app.services.invites import is_invite_valid
from app.services.notifications import notify
from .client_base import MediaClient, register_media_client

# Reuse the same email regex as jellyfin
EMAIL_RE = re.compile(r"[^@]+@[^@]+\.[^@]+")


@register_media_client("emby")
class EmbyClient(MediaClient):
    """Wrapper around the Emby REST API using credentials from Settings."""

    def __init__(self):
        super().__init__(url_key="server_url", token_key="api_key")

    @property
    def hdrs(self):
        return {"X-Emby-Token": self.token}

    def get(self, path: str):
        r = requests.get(f"{self.url}{path}", headers=self.hdrs, timeout=10)
        logging.info("GET  %s%s → %s", self.url, path, r.status_code)
        r.raise_for_status()
        return r

    def post(self, path: str, payload: dict):
        r = requests.post(
            f"{self.url}{path}",
            json=payload,
            headers=self.hdrs,
            timeout=10,
        )
        logging.info("POST %s%s → %s", self.url, path, r.status_code)
        r.raise_for_status()
        return r

    def delete(self, path: str):
        r = requests.delete(f"{self.url}{path}", headers=self.hdrs, timeout=10)
        logging.info("DEL  %s%s → %s", self.url, path, r.status_code)
        r.raise_for_status()
        return r

    def libraries(self) -> dict[str, str]:
        return {
            item["Id"]: item["Name"]
            for item in self.get("/Library/MediaFolders").json()["Items"]
        }

    def create_user(self, username: str, password: str) -> str:
        """Create user and set password"""
        # Step 1: Create user without password
        user = self.post("/Users/New", {"Name": username}).json()
        user_id = user["Id"]
        
        # Step 2: Set password
        try:
            logging.info(f"Setting password for user {username} (ID: {user_id})")
            password_response = self.post(
                f"/Users/{user_id}/Password",
                {
                    "NewPw": password,
                    "CurrentPw": "",  # No current password for new users
                    "ResetPassword": False  # Important! Don't reset password
                }
            )
            logging.info(f"Password set response: {password_response.status_code}")
        except Exception as e:
            logging.error(f"Failed to set password for user {username}: {str(e)}")
            # Continue with user creation even if password setting fails
            # as we may need to debug further
        
        return user_id
        
    def set_policy(self, user_id: str, policy: dict) -> None:
        self.post(f"/Users/{user_id}/Policy", policy)

    def delete_user(self, user_id: str) -> None:
        self.delete(f"/Users/{user_id}")

    def get_user(self, emby_id: str) -> dict:
        return self.get(f"/Users/{emby_id}").json()

    def update_user(self, emby_id: str, form: dict) -> dict | None:
        current = self.get_user(emby_id)

        for key, val in form.items():
            for section in ("Policy", "Configuration"):
                if key in current[section]:
                    target = current[section][key]
                    if isinstance(target, bool):
                        val = (val == "True")
                    elif isinstance(target, int):
                        val = int(val)
                    elif isinstance(target, list):
                        val = [] if val == "" else val.split(", ")
                    current[section][key] = val

        return self.post(f"/Users/{emby_id}", current).json()

    def list_users(self) -> list[User]:
        """Sync users from Emby into the local DB and return the list of User records."""
        emby_users = {u["Id"]: u for u in self.get("/Users").json()}

        for emby in emby_users.values():
            existing = User.query.filter_by(token=emby["Id"]).first()
            if not existing:
                new = User(
                    token=emby["Id"],
                    username=emby["Name"],
                    email="empty",
                    code="empty",
                    password="empty",
                ) 
                db.session.add(new)
        db.session.commit()

        for dbu in User.query.all():
            if dbu.token not in emby_users:
                db.session.delete(dbu)
        db.session.commit()

        return User.query.all()


# Helper functions for Emby operations


def mark_invite_used(inv: Invitation, user: User) -> None:
    """Mark an invitation as used by a specific user"""
    inv.used = True if not inv.unlimited else inv.used
    inv.used_at = datetime.datetime.now()
    inv.used_by = user
    db.session.commit()


def folder_name_to_id(name: str, cache: dict[str, str]) -> str | None:
    """Convert a folder ID or name to its ID
    
    Args:
        name: Either a library name or a library ID
        cache: Dictionary mapping library IDs to library names
        
    Returns:
        The library ID if found, None otherwise
    """
    # Check if the name is already a valid ID
    if name in cache:
        return name
    
    # Otherwise, try to find the ID by name
    for folder_id, folder_name in cache.items():
        if folder_name == name:
            return folder_id
    
    # Not found
    return None


def set_specific_folders(client: EmbyClient, user_id: str, names: list[str]):
    """Set specific folders for a user based on selected libraries
    
    Args:
        client: The Emby client instance
        user_id: The ID of the user to set permissions for
        names: List of library external_ids to enable for the user
    """
    # Following the same simple approach as the working Jellyfin implementation
    logging.info(f"Setting folder access for user {user_id} with libraries: {names}")
    
    # Get all media folders
    response = client.get("/Library/MediaFolders")
    media_folders = response.json().get("Items", [])
    
    # Create mapping (match Jellyfin's approach with both ID→Name and Name→ID)
    id_to_name = {item["Id"]: item["Name"] for item in media_folders}
    name_to_id = {item["Name"]: item["Id"] for item in media_folders}
    
    # Debug info
    logging.info(f"Available libraries: {', '.join([f'{id}: {name}' for id, name in id_to_name.items()])}")
    
    # Convert names to IDs, handling both cases where names could be IDs or actual names
    folder_ids = []
    for name in names:
        # If it's already an ID
        if name in id_to_name:
            folder_ids.append(name)
            logging.info(f"Found direct ID match for {name}: {id_to_name[name]}")
        # If it's a name
        elif name in name_to_id:
            folder_ids.append(name_to_id[name])
            logging.info(f"Found name match for {name}: {name_to_id[name]}")
        else:
            logging.warning(f"Could not find library matching: {name}")
    
    # Remove duplicates and None values
    folder_ids = list(set([fid for fid in folder_ids if fid]))
    
    # Log what we found
    if folder_ids:
        logging.info(f"Matched {len(folder_ids)} libraries: {[id_to_name.get(fid, fid) for fid in folder_ids]}")
    else:
        logging.warning("No matching libraries found, user will have no access")
    
    # Create simple policy patch - IDENTICAL to Jellyfin implementation
    policy_patch = {
        "EnableAllFolders": not folder_ids,  # True if empty list, False otherwise
        "EnabledFolders": folder_ids,
    }
    
    # Get current policy
    current = client.get_user(user_id)["Policy"]
    
    # Log what we're doing
    logging.info(f"Setting EnableAllFolders={policy_patch['EnableAllFolders']}")
    logging.info(f"Setting EnabledFolders={policy_patch['EnabledFolders']}")
    
    # Update current policy with our changes
    current.update(policy_patch)
    
    # Make sure essential playback permissions are enabled
    playback_permissions = {
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "EnablePlaybackRemuxing": True,
        "EnableContentDownloading": True,
        "EnableRemoteAccess": True,
    }
    current.update(playback_permissions)
    
    # Apply the updated policy
    client.set_policy(user_id, current)


def join(username: str, password: str, confirm: str, email: str, code: str) -> tuple[bool, str]:
    """Process a join request for a new Emby user"""
    client = EmbyClient()
    
    # Validate input data
    if not EMAIL_RE.fullmatch(email):
        return False, "Invalid e-mail address."
    if not 8 <= len(password) <= 20:
        return False, "Password must be 8–20 characters."
    if password != confirm:
        return False, "Passwords do not match."
    
    # Validate invitation code
    ok, msg = is_invite_valid(code)
    if not ok:
        return False, msg
        
    # Check for existing users with same username/email
    existing = User.query.filter(
        or_(User.username == username, User.email == email)
    ).first()
    if existing:
        return False, "User or e-mail already exists."
    
    try:
        # Create the user in Emby
        logging.info(f"Creating Emby user: {username}")
        
        # Step 1: Create the user in Emby
        user_id = client.create_user(username, password)
        logging.info(f"Emby user created with ID: {user_id}")
        
        # Step 2: Get invitation record to determine library access
        inv = Invitation.query.filter_by(code=code).first()
        
        # Step 3: Determine which libraries to grant access to
        if inv.libraries:
            logging.info(f"Using specific libraries from invitation: {[lib.name for lib in inv.libraries]}")
            sections = [lib.external_id for lib in inv.libraries]
        else:
            logging.info("No specific libraries in invitation, using all enabled libraries")
            sections = [
                lib.external_id
                for lib in Library.query.filter_by(enabled=True).all()
            ]
        
        logging.info(f"Library IDs to enable: {sections}")
            
        # Step 4: Apply folder permissions directly (skip initial policy)
        # This avoids any potential conflicts between multiple policy updates
        set_specific_folders(client, user_id, sections)
        logging.info(f"Applied library permissions for Emby user: {username}")
        
        # Calculate expiration date if needed
        expires = None
        if inv.duration:
            days = int(inv.duration)
            expires = datetime.datetime.utcnow() + datetime.timedelta(days=days)
        
        # Create local user record
        new_user = User(
            username=username,
            email=email,
            password="emby-user",  # Not used for auth, just a placeholder
            token=user_id,
            code=code,
            expires=expires,
        )
        
        db.session.add(new_user)
        db.session.commit()
        
        # Mark invitation as used
        mark_invite_used(inv, new_user)
        notify("New User", f"User {username} has joined your Emby server! 🎉", tags="tada")
        
        # Return success
        return True, ""
        
    except Exception as e:
        logging.error(f"Emby join error: {str(e)}", exc_info=True)
        db.session.rollback()
        return False, "An unexpected error occurred during user creation."
