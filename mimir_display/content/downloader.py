"""
Content Download Manager for MQTT Assignments

Handles downloading content from URLs with SHA256 validation and caching.
Supports the URL-based approach recommended in the migration plan.
"""

import hashlib
import logging
import aiohttp
import asyncio
import os
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timezone


class ContentDownloader:
    """Manages content download with caching and validation.

    Cache directory resolution enhancement (2025-09-23):
        Previous implementation stored cache under ``Path.home()/.mimir/content_cache`` which
        fails with systemd hardening (ProtectHome=true). We now resolve using:
            1. Explicit ``cache_dir`` argument
            2. Environment variable ``MIMIR_CACHE_DIR`` (preferred for packaging)
            3. Environment variable ``DATA_DIR`` + 'cache'
            4. ``/var/lib/mimir-display/cache`` (standard runtime location)
            5. Legacy fallback ``Path.home()/.mimir/content_cache``

        If a candidate is not writable it is skipped; the chosen directory is logged at INFO.
    """
    
    def __init__(self, cache_dir: Path = None, timeout: int = 30):
        self.logger = logging.getLogger(__name__)
        self.cache_dir = self._resolve_cache_dir(cache_dir)
        self.timeout = timeout

    def _resolve_cache_dir(self, explicit: Optional[Path]) -> Path:
        """Resolve a writable cache directory with precedence and fallbacks."""
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit))

        env_cache = os.getenv("MIMIR_CACHE_DIR")
        if env_cache:
            candidates.append(Path(env_cache))

        data_dir = os.getenv("DATA_DIR")
        if data_dir:
            candidates.append(Path(data_dir) / "cache")

        candidates.append(Path("/var/lib/mimir-display/cache"))
        candidates.append(Path.home() / ".mimir" / "content_cache")  # legacy fallback

        for c in candidates:
            try:
                c.mkdir(parents=True, exist_ok=True)
                test_file = c / ".write_test"
                with open(test_file, "w") as f:
                    f.write("ok")
                test_file.unlink(missing_ok=True)
                self.logger.info(f"Using content cache directory: {c}")
                return c
            except Exception as e:  # PermissionError or other OSErrors
                self.logger.warning(f"Cache directory not usable ({c}): {e}")
                continue

        # Last resort: current working directory
        fallback = Path.cwd() / "cache"
        fallback.mkdir(parents=True, exist_ok=True)
        self.logger.error(f"All cache directory candidates failed; using {fallback}")
        return fallback
    
    def _sha256_file(self, file_path: Path) -> str:
        """Calculate SHA256 hash of a file."""
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    
    def _get_cache_path(self, content_id: str, expected_sha: str = None) -> Path:
        """Get cache file path for content."""
        if expected_sha:
            # Use SHA256 as filename for content-addressed caching
            return self.cache_dir / expected_sha
        else:
            # Fallback to content ID
            return self.cache_dir / content_id
        
    def _normalize_delivery(self, assignment: dict) -> dict:
        """Return a canonical {'type':'url','url':..., 'content_type':...} or raise KeyError('url')."""
        a = assignment or {}

        # canonical: assignment['content']['delivery']['url']
        d = a.get("content", {}).get("delivery")
        if isinstance(d, dict) and d.get("url"):
            return d

        # alt: assignment['delivery']['url']
        d = a.get("delivery")
        if isinstance(d, dict) and d.get("url"):
            return d

        # alt: assignment['content_url']  / assignment['image_url']
        if "content_url" in a:
            return {"type": "url", "url": a["content_url"], "content_type": a.get("content_type")}
        if "image_url" in a:
            return {"type": "url", "url": a["image_url"], "content_type": a.get("content_type")}

        raise KeyError("url")        
    
    async def download_with_cache(
        self, 
        url: str, 
        content_id: str,
        expected_sha: str = None,
        force_download: bool = False
    ) -> Path:
        """
        Download content with caching and validation.
        
        Args:
            url: URL to download from
            content_id: Unique identifier for the content
            expected_sha: Expected SHA256 hash for validation
            force_download: Skip cache and force fresh download
            
        Returns:
            Path to the downloaded/cached file
            
        Raises:
            ValueError: If SHA256 validation fails
            aiohttp.ClientError: If download fails
        """
        cache_path = self._get_cache_path(content_id, expected_sha)
        
        # Check if file exists in cache and is valid
        if not force_download and cache_path.exists():
            if expected_sha:
                actual_sha = self._sha256_file(cache_path)
                if actual_sha == expected_sha:
                    self.logger.debug(f"Cache hit for {content_id} ({expected_sha[:8]}...)")
                    return cache_path
                else:
                    self.logger.warning(f"Cache SHA mismatch for {content_id}, re-downloading")
            else:
                self.logger.debug(f"Cache hit for {content_id} (no SHA validation)")
                return cache_path
        
        # Download the file
        self.logger.info(f"Downloading {content_id} from {url}")
        start_time = datetime.now()
        
        temp_path = cache_path.with_suffix('.tmp')
        
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    
                    # Stream download to temporary file
                    with open(temp_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
            
            # Validate SHA256 if provided
            if expected_sha:
                actual_sha = self._sha256_file(temp_path)
                if actual_sha != expected_sha:
                    temp_path.unlink()  # Clean up invalid file
                    raise ValueError(f"SHA256 mismatch: expected {expected_sha}, got {actual_sha}")
                
                self.logger.debug(f"SHA256 validation passed for {content_id}")
            
            # Move to final location
            temp_path.rename(cache_path)
            
            duration = (datetime.now() - start_time).total_seconds()
            file_size = cache_path.stat().st_size
            self.logger.info(f"Downloaded {content_id}: {file_size} bytes in {duration:.2f}s")
            
            return cache_path
            
        except Exception as e:
            # Clean up temporary file on error
            if temp_path.exists():
                temp_path.unlink()
            
            self.logger.error(f"Download failed for {content_id}: {e}")
            raise
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about the current cache."""
        if not self.cache_dir.exists():
            return {"total_files": 0, "total_size": 0, "cache_dir": str(self.cache_dir)}
        
        files = list(self.cache_dir.glob("*"))
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        
        return {
            "total_files": len(files),
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_dir": str(self.cache_dir)
        }
    
    def clear_cache(self, keep_recent: int = 0) -> int:
        """
        Clear the content cache.
        
        Args:
            keep_recent: Number of most recent files to keep (0 = clear all)
            
        Returns:
            Number of files removed
        """
        if not self.cache_dir.exists():
            return 0
        
        files = sorted(
            [f for f in self.cache_dir.glob("*") if f.is_file()],
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        
        files_to_remove = files[keep_recent:] if keep_recent > 0 else files
        
        removed_count = 0
        for file_path in files_to_remove:
            try:
                file_path.unlink()
                removed_count += 1
            except Exception as e:
                self.logger.warning(f"Failed to remove cache file {file_path}: {e}")
        
        if removed_count > 0:
            self.logger.info(f"Cleared {removed_count} files from content cache")
        
        return removed_count

    def _normalize_delivery(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        """Accept several payload shapes and return a unified delivery dict."""
        a = assignment or {}

        # 1) Canonical
        d = a.get("content", {}).get("delivery")
        if isinstance(d, dict) and d.get("url"):
            return d

        # 2) Top-level delivery
        d = a.get("delivery")
        if isinstance(d, dict) and d.get("url"):
            return d

        # 3) Legacy/API variants
        if "content_url" in a:
            return {"type": "url", "url": a["content_url"], "content_type": a.get("content_type")}
        if "image_url" in a:
            return {"type": "url", "url": a["image_url"], "content_type": a.get("content_type")}

        raise KeyError("url")  # keep the same error type but now it means truly missing

    async def process_assignment(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info("Processing assignment %s", assignment.get("assignment_id"))
        try:
            self._normalize_delivery(assignment)  # validation only; legacy path retained
            # url/content_type extraction retained for compatibility if later needed
            # (Removed unused local variables to satisfy linter.)
        except KeyError as e:
            # This was a shape issue before; now only log it at WARNING
            self.logger.warning("Assignment %s missing %s", assignment.get("assignment_id"), e)
            return {
                "assignment_id": assignment.get("assignment_id"),
                "sequence": assignment.get("sequence"),
                "success": False,
                "error": str(e),
                "error_type": "KeyError",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            self.logger.exception("Assignment %s processing failed", assignment.get("assignment_id"))
            return {
                "assignment_id": assignment.get("assignment_id"),
                "sequence": assignment.get("sequence"),
                "success": False,
                "error": str(e),
                "error_type": "processing_failed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }


class AssignmentProcessor:
    """Processes MQTT assignment commands and manages content workflow."""
    
    def __init__(self, downloader: ContentDownloader, display_callback=None):
        self.downloader = downloader
        self.display_callback = display_callback  # Function to call with processed content
        self.logger = logging.getLogger(__name__)
    
    async def process_assignment(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an assignment command and return result info.
        
        Args:
            assignment: MQTT assignment command payload
            
        Returns:
            Dict with processing results and metadata
        """
        assignment_id = assignment.get("assignment_id")
        asset = assignment.get("asset") or {}
        display_config = assignment.get("display", {})
        sequence = assignment.get("sequence")
        
        self.logger.info("Processing assignment %s", assignment_id)
        
        try:
            # Backward compatibility / normalization: allow alternate shapes
            if not asset or "url" not in asset:
                # Try delivery paths similar to ContentDownloader normalization
                delivery = assignment.get("content", {}).get("delivery") or assignment.get("delivery")
                url_candidate = None
                if isinstance(delivery, dict):
                    url_candidate = delivery.get("url")
                if not url_candidate:
                    url_candidate = assignment.get("image_url") or assignment.get("content_url")
                if not url_candidate:
                    raise KeyError("url")

                # Derive an ID from basename (strip query) or fall back to assignment id
                base_part = url_candidate.split("?")[0].rsplit("/", 1)[-1] or assignment_id or "asset"
                asset = {
                    "id": base_part,
                    "url": url_candidate,
                }

            # Download content
            content_path = await self.downloader.download_with_cache(
                url=asset["url"],
                content_id=asset["id"],
                expected_sha=asset.get("sha256")
            )
            
            # Process display configuration
            result = {
                "assignment_id": assignment_id,
                "sequence": sequence,
                "content_path": str(content_path),
                "asset_id": asset["id"],
                "display_config": display_config,
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "success": True
            }
            
            # Call display callback if provided
            if self.display_callback:
                try:
                    display_result = await self._call_display_callback(content_path, display_config)
                    result["display_result"] = display_result
                except Exception as e:
                    self.logger.error(f"Display callback failed for {assignment_id}: {e}")
                    result["display_error"] = str(e)
            
            self.logger.info("Assignment %s processed successfully", assignment_id)
            return result
            
        except Exception as e:
            self.logger.error("Assignment %s processing failed: %s", assignment_id, e)
            return {
                "assignment_id": assignment_id,
                "sequence": sequence,
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
    
    async def _call_display_callback(self, content_path: Path, display_config: Dict[str, Any]):
        """Call the display callback function."""
        if asyncio.iscoroutinefunction(self.display_callback):
            return await self.display_callback(content_path, display_config)
        else:
            return self.display_callback(content_path, display_config)
