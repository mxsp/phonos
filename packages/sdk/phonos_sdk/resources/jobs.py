"""Jobs resource for Phonos SDK."""
from typing import Optional, Dict, Any, List


class JobsResource:
    """Manage computational jobs."""

    def __init__(self, client):
        self.client = client

    def submit(
        self,
        project_id: int,
        model_id: str,
        sequence: Optional[str] = None,
        file_id: Optional[int] = None,
        job_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a new computational job.

        Args:
            project_id: ID of the project
            model_id: Model to use (e.g., 'type-0' for ESMFold)
            sequence: Input sequence (for sequence-based models)
            file_id: File ID (for file-based models)
            job_name: Optional name for the job

        Returns:
            Job information dict with job_id

        Example:
            >>> job = client.jobs.submit(
            ...     project_id=1,
            ...     model_id="type-0",
            ...     sequence="MKTAYIAKQRQISFVK..."
            ... )
            >>> print(job['job_id'])
        """
        data = {
            'project_id': project_id,
            'model_id': model_id,
        }

        if sequence:
            data['sequence'] = sequence
        if file_id:
            data['file_id'] = file_id
        if job_name:
            data['job_name'] = job_name

        return self.client._request('POST', '/submit_job', json=data)

    def list(self, project_id: int) -> List[Dict[str, Any]]:
        """List all jobs for a project.

        Args:
            project_id: ID of the project

        Returns:
            List of job dicts

        Example:
            >>> jobs = client.jobs.list(project_id=1)
            >>> for job in jobs:
            ...     print(f"{job['id']}: {job['status']}")
        """
        response = self.client._request(
            'GET',
            f'/api/projects/{project_id}/jobs'
        )
        return response.get('jobs', [])

    def get(self, job_id: str) -> Dict[str, Any]:
        """Get job details.

        Args:
            job_id: Job ID

        Returns:
            Job details dict
        """
        # Note: The API doesn't have a specific GET endpoint for single job
        # This would need to parse from the job list or output view
        # For now, we return output content
        return self.client._request('GET', f'/api/jobs/{job_id}/output_content')

    def get_logs(self, job_id: str) -> Dict[str, Any]:
        """Get job logs.

        Args:
            job_id: Job ID

        Returns:
            Job logs dict

        Example:
            >>> logs = client.jobs.get_logs("job_abc123")
            >>> print(logs['logs'])
        """
        return self.client._request('GET', f'/api/jobs/{job_id}/logs')

    def retry(self, job_id: str) -> Dict[str, Any]:
        """Retry a failed job.

        Args:
            job_id: Job ID

        Returns:
            Response dict

        Example:
            >>> result = client.jobs.retry("job_abc123")
        """
        return self.client._request('POST', f'/api/jobs/{job_id}/retry')

    def delete(self, job_id: str) -> Dict[str, Any]:
        """Delete a job.

        Args:
            job_id: Job ID

        Returns:
            Response dict

        Example:
            >>> client.jobs.delete("job_abc123")
        """
        return self.client._request('DELETE', f'/api/jobs/{job_id}')

    def rename(self, job_id: str, new_name: str) -> Dict[str, Any]:
        """Rename a job.

        Args:
            job_id: Job ID
            new_name: New job name

        Returns:
            Response dict

        Example:
            >>> client.jobs.rename("job_abc123", "My Analysis")
        """
        return self.client._request(
            'PATCH',
            f'/api/jobs/{job_id}/rename',
            json={'new_name': new_name}
        )

    def download_output(self, job_id: str, output_path: str) -> None:
        """Download job output file.

        Args:
            job_id: Job ID
            output_path: Local path to save the file

        Example:
            >>> client.jobs.download_output("job_abc123", "result.pdb")
        """
        url = f"{self.client.base_url}/download_output/{job_id}"
        response = self.client.session.get(url, timeout=self.client.timeout)
        response.raise_for_status()

        with open(output_path, 'wb') as f:
            f.write(response.content)

    def download_artifact(self, job_id: str, artifact_name: str, output_path: str) -> None:
        """Download specific job artifact.

        Args:
            job_id: Job ID
            artifact_name: Name of the artifact
            output_path: Local path to save the file

        Example:
            >>> client.jobs.download_artifact("job_abc123", "plot.png", "plot.png")
        """
        url = f"{self.client.base_url}/download_job_artifact/{job_id}/{artifact_name}"
        response = self.client.session.get(url, timeout=self.client.timeout)
        response.raise_for_status()

        with open(output_path, 'wb') as f:
            f.write(response.content)

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all jobs across all projects.

        Returns:
            List of all jobs

        Example:
            >>> all_jobs = client.jobs.get_all()
        """
        response = self.client._request('GET', '/api/jobs/all')
        return response.get('jobs', [])
