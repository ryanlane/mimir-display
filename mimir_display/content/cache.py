"""
Image caching and management.

This module handles local image caching, cache validation,
and cache cleanup operations.
"""

import os
import time
import hashlib
from typing import Optional


class ImageCache:
    """
    Manages local image caching for display content.
    
    Provides intelligent caching with validation, expiry handling,
    and cleanup operations to optimize display performance and
    reduce network usage.
    """
    
    def __init__(self, cache_dir: str, logger):
        """
        Initialize image cache.
        
        Args:
            cache_dir: Directory for cache storage
            logger: Logger instance
        """
        self.cache_dir = cache_dir
        self.logger = logger
        os.makedirs(cache_dir, exist_ok=True)
    
    def _cache_path(self, key: str) -> str:
        """
        Generate cache file path for a given key.
        
        Args:
            key: Cache key (checksum or URL)
            
        Returns:
            Full path to cache file
        """
        # Ensure filename is safe by hashing
        safe_key = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{safe_key}.png")
    
    def get(self, key: str, max_age: Optional[float] = None) -> Optional[bytes]:
        """
        Get cached image data if valid.
        
        Args:
            key: Cache key
            max_age: Maximum age in seconds (None for no expiry check)
            
        Returns:
            Image data if cached and valid, None otherwise
        """
        cache_path = self._cache_path(key)
        
        if not os.path.exists(cache_path):
            return None
        
        # Check age if specified
        if max_age is not None:
            age = time.time() - os.path.getmtime(cache_path)
            if age > max_age:
                self.logger.debug("Cache expired for key: %s (age: %.1fs)", key, age)
                return None
        
        try:
            with open(cache_path, "rb") as f:
                data = f.read()
            self.logger.debug("Cache hit for key: %s", key)
            return data
        except Exception as e:
            self.logger.warning("Failed to read cache for key %s: %s", key, e)
            return None
    
    def put(self, key: str, data: bytes) -> bool:
        """
        Store image data in cache.
        
        Args:
            key: Cache key
            data: Image data to cache
            
        Returns:
            True if successfully cached
        """
        cache_path = self._cache_path(key)
        
        try:
            # Use atomic write to prevent corruption
            temp_path = cache_path + ".tmp"
            with open(temp_path, "wb") as f:
                f.write(data)
            os.replace(temp_path, cache_path)
            
            self.logger.debug("Cached data for key: %s (%d bytes)", key, len(data))
            return True
            
        except Exception as e:
            self.logger.warning("Failed to cache data for key %s: %s", key, e)
            return False
    
    def exists(self, key: str) -> bool:
        """
        Check if key exists in cache.
        
        Args:
            key: Cache key
            
        Returns:
            True if key exists in cache
        """
        return os.path.exists(self._cache_path(key))
    
    def remove(self, key: str) -> bool:
        """
        Remove item from cache.
        
        Args:
            key: Cache key
            
        Returns:
            True if successfully removed
        """
        cache_path = self._cache_path(key)
        
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
                self.logger.debug("Removed cache for key: %s", key)
            return True
        except Exception as e:
            self.logger.warning("Failed to remove cache for key %s: %s", key, e)
            return False
    
    def cleanup(self, max_age_hours: int = 24) -> int:
        """
        Clean up old cache files.
        
        Args:
            max_age_hours: Maximum age in hours before cleanup
            
        Returns:
            Number of files cleaned up
        """
        max_age_seconds = max_age_hours * 3600
        current_time = time.time()
        cleaned_count = 0
        
        try:
            for filename in os.listdir(self.cache_dir):
                file_path = os.path.join(self.cache_dir, filename)
                
                if not os.path.isfile(file_path):
                    continue
                
                age = current_time - os.path.getmtime(file_path)
                if age > max_age_seconds:
                    try:
                        os.remove(file_path)
                        cleaned_count += 1
                        self.logger.debug("Cleaned up old cache file: %s", filename)
                    except Exception as e:
                        self.logger.warning("Failed to clean up %s: %s", filename, e)
            
            if cleaned_count > 0:
                self.logger.info("Cache cleanup: removed %d old files", cleaned_count)
                
        except Exception as e:
            self.logger.warning("Cache cleanup failed: %s", e)
        
        return cleaned_count
    
    def get_temp_path(self, suffix: str = "") -> str:
        """
        Get a temporary file path in the cache directory.
        
        Args:
            suffix: Optional suffix for the filename
            
        Returns:
            Temporary file path
        """
        timestamp = str(int(time.time() * 1000))
        filename = f"tmp_{timestamp}{suffix}"
        return os.path.join(self.cache_dir, filename)
