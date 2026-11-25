"""API client for Phonos CLI."""
import requests
from typing import Any, Dict, Optional

from .config import get_api_key, get_endpoint


class PhonosAPIError(Exception):
    """Exception raised for API errors."""
    pass


class PhonosClient:
    """Client for interacting with Phonos API."""

    def __init__(self, api_key: Optional[str] = None, endpoint: Optional[str] = None):
        """Initialize the client."""
        self.api_key = api_key or get_api_key()
        self.endpoint = endpoint or get_endpoint()

        if not self.api_key:
            raise PhonosAPIError("API key not configured. Run 'phonos config set-api-key YOUR_KEY'")

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Make an API request."""
        url = f"{self.endpoint}{path}"

        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            try:
                error_data = e.response.json()
                error_message = error_data.get("message", str(e))
            except:
                error_message = str(e)

            raise PhonosAPIError(f"API Error: {error_message}")
        except requests.exceptions.RequestException as e:
            raise PhonosAPIError(f"Request failed: {str(e)}")

    def get(self, path: str, **kwargs) -> Dict[str, Any]:
        """Make a GET request."""
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Dict[str, Any]:
        """Make a POST request."""
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Dict[str, Any]:
        """Make a PUT request."""
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Dict[str, Any]:
        """Make a DELETE request."""
        return self._request("DELETE", path, **kwargs)

    # Project methods
    def list_projects(self):
        """List all projects."""
        return self.get("/api/projects")

    def get_project(self, project_id: int):
        """Get project details."""
        return self.get(f"/api/projects/{project_id}")

    def create_project(self, name: str, description: str = ""):
        """Create a new project."""
        return self.post("/api/projects", json={"name": name, "description": description})

    # Job methods
    def list_jobs(self, project_id: int):
        """List jobs in a project."""
        return self.get(f"/api/projects/{project_id}/jobs")

    def submit_job(self, project_id: int, model: str, sequence: str = None, file_id: int = None):
        """Submit a new job."""
        data = {
            "model": model,
            "sequence": sequence,
            "input_file_id": file_id,
        }
        return self.post(f"/api/projects/{project_id}/jobs", json=data)

    def get_job(self, job_id: str):
        """Get job details."""
        return self.get(f"/api/jobs/{job_id}")

    def download_job_result(self, job_id: str):
        """Download job result."""
        url = f"{self.endpoint}/api/jobs/{job_id}/download"
        response = self.session.get(url)
        response.raise_for_status()
        return response.content

    # File methods
    def list_files(self, project_id: int):
        """List files in a project."""
        return self.get(f"/api/projects/{project_id}/files")

    def upload_file(self, project_id: int, file_path: str, notes: str = ""):
        """Upload a file to a project."""
        with open(file_path, "rb") as f:
            files = {"file": f}
            data = {"notes": notes}
            url = f"{self.endpoint}/api/projects/{project_id}/files/upload"
            response = self.session.post(url, files=files, data=data)
            response.raise_for_status()
            return response.json()

    def download_file(self, file_id: int):
        """Download a file."""
        url = f"{self.endpoint}/api/files/{file_id}/download"
        response = self.session.get(url)
        response.raise_for_status()
        return response.content

    # Template methods
    def list_templates(self):
        """List all templates."""
        return self.get("/api/templates")

    def get_template(self, template_id: str):
        """Get template details."""
        return self.get(f"/api/templates/{template_id}")

    # Analytics methods
    def get_usage_summary(self):
        """Get usage summary."""
        return self.get("/api/analytics/summary")

    def get_daily_usage(self, days: int = 30):
        """Get daily usage statistics."""
        return self.get(f"/api/analytics/daily?days={days}")

    # Pipeline methods
    def list_pipelines(self, project_id: int):
        """List pipelines in a project."""
        return self.get(f"/api/projects/{project_id}/pipelines")

    def get_pipeline(self, pipeline_id: str):
        """Get pipeline details."""
        return self.get(f"/api/pipelines/{pipeline_id}")

    def create_pipeline(self, project_id: int, name: str, description: str, steps: list):
        """Create a new pipeline."""
        data = {
            "name": name,
            "description": description,
            "steps": steps,
        }
        return self.post(f"/api/projects/{project_id}/pipelines", json=data)

    # Search methods
    def search(self, query: str, categories: list = None):
        """Perform a global search."""
        params = {"q": query}
        if categories:
            params["categories"] = categories
        return self.get("/api/search", params=params)
