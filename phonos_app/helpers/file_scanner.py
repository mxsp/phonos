"""
File Upload Security Scanner
Provides malware detection, file type verification, and content analysis.
"""
from __future__ import annotations

import os
import hashlib
import mimetypes
from typing import Dict, Tuple, Optional, List
from pathlib import Path

from .audit_log import log_audit_event, AuditEventType
from .ip_security import IPSecurity, ThreatEvent


class FileScanResult:
    """Result of file security scan."""

    def __init__(self):
        self.is_safe = True
        self.threats = []
        self.warnings = []
        self.file_type = None
        self.file_hash = None
        self.size = 0

    def add_threat(self, threat: str):
        """Add a threat detection."""
        self.is_safe = False
        self.threats.append(threat)

    def add_warning(self, warning: str):
        """Add a warning."""
        self.warnings.append(warning)


class FileScanner:
    """File security scanner."""

    # Dangerous file extensions
    DANGEROUS_EXTENSIONS = {
        '.exe', '.dll', '.so', '.dylib',  # Executables
        '.bat', '.cmd', '.sh', '.ps1',    # Scripts
        '.com', '.pif', '.scr',           # System files
        '.vbs', '.vbe', '.js', '.jse',    # Scripts
        '.jar', '.class',                 # Java
        '.app', '.deb', '.rpm',           # Packages
        '.msi', '.dmg',                   # Installers
    }

    # Allowed extensions for this platform
    ALLOWED_EXTENSIONS = {
        # Documents
        '.txt', '.csv', '.tsv', '.json', '.yaml', '.yml', '.xml',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx',
        # Scientific
        '.pdb', '.cif', '.mmcif', '.mol', '.mol2', '.sdf', '.smi', '.smiles',
        '.fasta', '.fa', '.faa', '.fastq', '.gb', '.gbk',
        # Images
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp',
        # Archives
        '.zip', '.gz', '.tar', '.bz2',
        # Code
        '.py', '.ipynb', '.r', '.jl',
        # Wolfram Language notebooks and source files
        '.nb', '.wl', '.wls', '.m', '.mx', '.wxf', '.wdx',
    }

    # Magic bytes for file type detection
    MAGIC_BYTES = {
        'pdf': b'%PDF',
        'zip': b'PK\x03\x04',
        'gzip': b'\x1f\x8b',
        'png': b'\x89PNG\r\n\x1a\n',
        'jpg': b'\xff\xd8\xff',
        'exe': b'MZ',
        'elf': b'\x7fELF',
    }

    # Malicious file signatures (known malware patterns)
    MALWARE_SIGNATURES = {
        # EICAR test file
        'eicar': b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*',
        # Common exploit patterns
        'pdf_exploit': b'/JavaScript',
        'office_macro': b'macro',
    }

    @classmethod
    def scan_file(cls, file_path: str, original_filename: str) -> FileScanResult:
        """
        Comprehensive file security scan.

        Args:
            file_path: Path to uploaded file
            original_filename: Original filename from upload

        Returns:
            FileScanResult with scan results
        """
        result = FileScanResult()

        if not os.path.exists(file_path):
            result.add_threat("File does not exist")
            return result

        # Get file stats
        result.size = os.path.getsize(file_path)

        # Check file extension
        ext = Path(original_filename).suffix.lower()
        if ext in cls.DANGEROUS_EXTENSIONS:
            result.add_threat(f"Dangerous file extension: {ext}")

        if ext and ext not in cls.ALLOWED_EXTENSIONS:
            result.add_warning(f"Uncommon file extension: {ext}")

        # Calculate file hash
        result.file_hash = cls._calculate_hash(file_path)

        # Check against known malware hashes
        if cls._is_known_malware_hash(result.file_hash):
            result.add_threat(f"Known malware hash: {result.file_hash}")

        # Detect file type from content
        detected_type = cls._detect_file_type(file_path)
        result.file_type = detected_type

        # Verify file extension matches content
        if not cls._verify_extension_matches_content(ext, detected_type):
            result.add_threat(f"File extension mismatch: {ext} vs {detected_type}")

        # Scan for malware signatures
        malware_found = cls._scan_for_malware_signatures(file_path)
        for malware in malware_found:
            result.add_threat(f"Malware signature detected: {malware}")

        # Check file size
        if result.size > 500 * 1024 * 1024:  # 500MB
            result.add_warning(f"Large file size: {result.size} bytes")

        # Check for suspicious content
        if cls._has_suspicious_content(file_path, ext):
            result.add_threat("Suspicious content detected in file")

        return result

    @staticmethod
    def _calculate_hash(file_path: str) -> str:
        """Calculate SHA256 hash of file."""
        sha256 = hashlib.sha256()

        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                sha256.update(chunk)

        return sha256.hexdigest()

    @staticmethod
    def _is_known_malware_hash(file_hash: str) -> bool:
        """Check if hash is in known malware database."""
        # In production, this would check against a database
        # of known malware hashes (e.g., VirusTotal API)
        known_malware_hashes = {
            '275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f',  # EICAR test
        }

        return file_hash in known_malware_hashes

    @classmethod
    def _detect_file_type(cls, file_path: str) -> Optional[str]:
        """Detect file type from magic bytes."""
        try:
            with open(file_path, 'rb') as f:
                header = f.read(16)

            for file_type, magic in cls.MAGIC_BYTES.items():
                if header.startswith(magic):
                    return file_type

            # Try mime type detection
            mime_type, _ = mimetypes.guess_type(file_path)
            return mime_type

        except Exception:
            return None

    @classmethod
    def _verify_extension_matches_content(cls, extension: str, detected_type: Optional[str]) -> bool:
        """Verify file extension matches detected content type."""
        if not detected_type:
            return True  # Can't verify

        ext_to_type = {
            '.pdf': 'pdf',
            '.zip': 'zip',
            '.gz': 'gzip',
            '.png': 'png',
            '.jpg': 'jpg',
            '.jpeg': 'jpg',
        }

        expected_type = ext_to_type.get(extension)
        if expected_type and detected_type != expected_type:
            return False

        return True

    @classmethod
    def _scan_for_malware_signatures(cls, file_path: str) -> List[str]:
        """Scan file for known malware signatures."""
        found_malware = []

        try:
            with open(file_path, 'rb') as f:
                content = f.read(1024 * 1024)  # Read first 1MB

            for malware_name, signature in cls.MALWARE_SIGNATURES.items():
                if signature in content:
                    found_malware.append(malware_name)

        except Exception:
            pass

        return found_malware

    @staticmethod
    def _has_suspicious_content(file_path: str, extension: str) -> bool:
        """Check for suspicious content patterns."""
        # Only check text-based files
        text_extensions = {'.txt', '.py', '.js', '.html', '.xml', '.json', '.yaml', '.yml'}

        if extension not in text_extensions:
            return False

        suspicious_patterns = [
            b'<script',
            b'eval(',
            b'exec(',
            b'__import__',
            b'system(',
            b'popen(',
            b'subprocess',
            b'os.system',
            b'os.popen',
        ]

        try:
            with open(file_path, 'rb') as f:
                content = f.read(1024 * 1024)  # Read first 1MB

            content_lower = content.lower()
            for pattern in suspicious_patterns:
                if pattern.lower() in content_lower:
                    return True

        except Exception:
            pass

        return False


def scan_upload(file_path: str, original_filename: str, user_id: int, ip_address: str) -> Tuple[bool, str]:
    """
    Scan uploaded file and log results.

    Args:
        file_path: Path to uploaded file
        original_filename: Original filename
        user_id: User ID who uploaded
        ip_address: IP address of uploader

    Returns:
        Tuple of (is_safe, message)
    """
    result = FileScanner.scan_file(file_path, original_filename)

    # Log scan results
    log_audit_event(
        AuditEventType.FILE_UPLOADED,
        f"File upload scanned: {original_filename}",
        result='safe' if result.is_safe else 'blocked',
        severity='info' if result.is_safe else 'error',
        user_id=user_id,
        details={
            'filename': original_filename,
            'size': result.size,
            'hash': result.file_hash,
            'type': result.file_type,
            'threats': result.threats,
            'warnings': result.warnings
        }
    )

    # If threats found, record security event and delete file
    if not result.is_safe:
        IPSecurity.record_threat_event(
            ip_address,
            'malware_upload',
            ThreatEvent.MALWARE_UPLOAD,
            details=f"File: {original_filename}, Threats: {', '.join(result.threats)}"
        )

        # Delete the malicious file
        try:
            os.remove(file_path)
        except Exception:
            pass

        threat_msg = '; '.join(result.threats)
        return False, f"File upload blocked: {threat_msg}"

    # Log warnings if any
    if result.warnings:
        warning_msg = '; '.join(result.warnings)
        return True, f"File uploaded with warnings: {warning_msg}"

    return True, "File is safe"


class FileQuarantine:
    """Quarantine system for suspicious files."""

    QUARANTINE_DIR = '/tmp/phonos_quarantine'

    @classmethod
    def quarantine_file(cls, file_path: str, reason: str, user_id: int) -> str:
        """
        Move file to quarantine.

        Returns:
            Quarantine ID
        """
        import shutil
        from datetime import datetime

        # Create quarantine directory
        os.makedirs(cls.QUARANTINE_DIR, exist_ok=True)

        # Generate quarantine ID
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        file_hash = FileScanner._calculate_hash(file_path)
        quarantine_id = f"{timestamp}_{file_hash[:8]}"

        # Move file to quarantine
        quarantine_path = os.path.join(cls.QUARANTINE_DIR, quarantine_id)
        shutil.move(file_path, quarantine_path)

        # Log quarantine
        log_audit_event(
            AuditEventType.ADMIN_ACTION,
            f"File quarantined: {quarantine_id}",
            severity='warning',
            user_id=user_id,
            details={
                'quarantine_id': quarantine_id,
                'reason': reason,
                'original_path': file_path
            }
        )

        return quarantine_id

    @classmethod
    def list_quarantined_files(cls) -> List[Dict]:
        """List all files in quarantine."""
        if not os.path.exists(cls.QUARANTINE_DIR):
            return []

        files = []
        for filename in os.listdir(cls.QUARANTINE_DIR):
            file_path = os.path.join(cls.QUARANTINE_DIR, filename)
            stat = os.stat(file_path)

            files.append({
                'quarantine_id': filename,
                'size': stat.st_size,
                'quarantined_at': stat.st_ctime,
                'path': file_path
            })

        return files

    @classmethod
    def release_from_quarantine(cls, quarantine_id: str, destination: str) -> bool:
        """Release file from quarantine."""
        import shutil

        quarantine_path = os.path.join(cls.QUARANTINE_DIR, quarantine_id)

        if not os.path.exists(quarantine_path):
            return False

        shutil.move(quarantine_path, destination)
        return True

    @classmethod
    def delete_quarantined(cls, quarantine_id: str) -> bool:
        """Permanently delete quarantined file."""
        quarantine_path = os.path.join(cls.QUARANTINE_DIR, quarantine_id)

        if not os.path.exists(quarantine_path):
            return False

        os.remove(quarantine_path)
        return True
