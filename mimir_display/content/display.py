"""
Display operations and image processing.

This module handles image processing, resizing, and display operations
for the e-ink display hardware.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from PIL import Image, ImageOps  # type: ignore

# Unified hardware abstraction (selects correct backend automatically)
from mimir_display.hardware import HARDWARE_AVAILABLE, display_image as hw_display_image


class DisplayManager:
    """
    Minimal display manager that loads an image from disk, fits it to the
    device resolution/orientation, and pushes it to the hardware backend.
    """
    
    def __init__(self, capabilities: dict[str, Any], cache_dir: str, logger):
        self.capabilities = capabilities or {}
        self.cache_dir = cache_dir
        self.logger = logger

        # Capabilities: {"resolution": [w, h], "orientation": "landscape|portrait|square", ...}
        # Logical (possibly swapped) resolution for upstream
        res = self.capabilities.get("resolution") or self.capabilities.get("size")
        if isinstance(res, (list, tuple)) and len(res) == 2:
            self.native_w, self.native_h = int(res[0]), int(res[1])
        else:
            # Reasonable default if caps are missing
            self.native_w, self.native_h = 800, 480

        self.orientation = (self.capabilities.get("orientation") or "landscape").lower()
        self.rotation_deg = int(self.capabilities.get("rotation_deg", 0))
        # Keep original logical resolution separate for calculations
        self.logical_w, self.logical_h = self.native_w, self.native_h
        # If caller provided native_resolution (always landscape ordering), store it
        nr = self.capabilities.get("native_resolution")
        if isinstance(nr, (list, tuple)) and len(nr) == 2:
            self.hw_w, self.hw_h = int(nr[0]), int(nr[1])
        else:
            self.hw_w, self.hw_h = self.logical_w, self.logical_h

    # ---- Public API expected by the rest of the app ----
    def display_from_file(self, path: str | Path) -> None:
        """
        Load an image file, fit to panel, and display it via the hardware backend.
        """
        path = str(path)
        self.logger.info("DisplayManager: rendering %s", path)

        # 1) Load
        img = Image.open(path)
        img = img.convert("RGB")  # ensure a sane mode for Inky drivers

        # 2) Fit to target resolution/orientation
        target_w, target_h = self._target_resolution()
        img = self._fit_image(img, target_w, target_h)

        # 3) Rotate according to physical orientation so hardware sees correct orientation
        if self.rotation_deg:
            img = img.rotate(self.rotation_deg, expand=True)

            # After rotation, hardware expects landscape native (hw_w x hw_h). If dimensions mismatch
            # due to potential non-square letterboxing, resize/crop letterbox again to native hardware.
            if (img.width, img.height) != (self.hw_w, self.hw_h):
                img = self._fit_image(img, self.hw_w, self.hw_h)

        # 4) Send to hardware
        self._hw_display_image(img)

        # Mark the source file as most-recent to protect it during pruning
        try:
            if str(path).startswith(self.cache_dir) and os.path.isfile(path):
                os.utime(path, None)
        except Exception:
            pass

        # Enforce retention each time we display
        self._enforce_cache_retention(keep=3)
    
    def resize_for_display(self, img: Image.Image) -> Image.Image:
        """
        Resize image to fit display resolution.
        
        Args:
            img: Source image
            
        Returns:
            Resized image that fits display
        """
        w, h = self._target_resolution()
        
        if img.size == (w, h):
            return img
        
        # Convert to RGB and letterbox to maintain aspect ratio
        img = img.convert("RGB")
        img = img.copy()
        img.thumbnail((w, h), Image.Resampling.LANCZOS)
        
        # Create canvas and center the image
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        x = (w - img.width) // 2
        y = (h - img.height) // 2
        canvas.paste(img, (x, y))
        
        return canvas
    
    def process_image_data(self, data: bytes) -> str:
        """
        Process image data and prepare for display.
        
        Args:
            data: Raw image data
            
        Returns:
            Path to processed image file ready for display
        """
        # Save to temporary file
        temp_path = os.path.join(self.cache_dir, "tmp_display.png")
        with open(temp_path, "wb") as f:
            f.write(data)
        
        try:
            # Load, resize, and save processed image
            img = Image.open(temp_path)
            img = self.resize_for_display(img)
            img.save(temp_path)
            
            self.logger.debug("Processed image: %dx%d -> %dx%d",
                              img.size[0], img.size[1], *self._target_resolution())
            return temp_path
            
        except Exception as e:
            self.logger.error("Failed to process image: %s", e)
            raise
    
    def display_image(self, image_path: str):
        """Display image on hardware via unified backend abstraction.

        This replaces legacy inky-only direct calls so non e-ink backends (e.g.,
        hyperpixelsq) do not emit confusing '[DEV] Would display' logs. The
        selected backend is resolved by mimir_display.hardware at import time.
        """
        try:
            if HARDWARE_AVAILABLE:
                hw_display_image(image_path)
                self.logger.info("Displayed image: %s", image_path)
            else:
                self.logger.info("SIMULATION: Would display image: %s", image_path)
        except Exception as e:  # noqa: BLE001
            self.logger.error("Failed to display image: %s", e)
            raise
    
    def display_from_data(self, data: bytes):
        """
        Process and display image data.
        
        Args:
            data: Raw image data to process and display
        """
        processed_path = self.process_image_data(data)
        try:
            self.display_image(processed_path)
        finally:
            # Best-effort cleanup of processed temp file
            try:
                if os.path.exists(processed_path):
                    os.remove(processed_path)
            except Exception:
                pass
            # Opportunistic sweep of stale temp artifacts in cache dir
            self._cleanup_cache_temps()
            # Enforce retention after each update
            self._enforce_cache_retention(keep=3)
    
    def display_default_content(self, default_path: str):
        """
        Display default content if available.
        
        Args:
            default_path: Path to default content image
        """
        if not default_path or not os.path.exists(default_path):
            self.logger.info("No default content to display")
            return
        
        temp_path = None
        try:
            self.logger.info("Displaying default content: %s", default_path)

            # Process default content to fit display
            img = Image.open(default_path)
            img = self.resize_for_display(img)

            temp_path = os.path.join(self.cache_dir, "default_resized.png")
            img.save(temp_path)

            self.display_image(temp_path)

        except Exception as e:
            self.logger.warning("Failed to display default content: %s", e)
        finally:
            # Cleanup processed default image
            if temp_path:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
            self._cleanup_cache_temps()
            self._enforce_cache_retention(keep=3)

    # ---- Helpers ----
    def _target_resolution(self) -> tuple[int, int]:
        """Return logical target resolution that upstream (platform) expects.

        For portrait orientations this is the swapped version already provided
        in capabilities, so we just return logical_w/h. We do NOT swap here again.
        """
        return (self.logical_w, self.logical_h)

    # Backwards compatibility for older code expecting .resolution attribute
    @property
    def resolution(self) -> tuple[int, int]:  # pragma: no cover - simple passthrough
        return self._target_resolution()
    
    def _fit_image(self, img: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """
        Letterbox fit: preserve aspect ratio, pad with white.
        """
        try:
            # EXIF orientation, if any
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass


        # Compute scale while preserving aspect ratio
        img_ratio = img.width / img.height
        tgt_ratio = target_w / target_h

        if img_ratio > tgt_ratio:
            # image is wider -> fit width
            new_w = target_w
            new_h = max(1, int(round(target_w / img_ratio)))
        else:
            # image is taller -> fit height
            new_h = target_h
            new_w = max(1, int(round(target_h * img_ratio)))

        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        # Letterbox on white (inky looks best with white background)
        canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
        off_x = (target_w - new_w) // 2
        off_y = (target_h - new_h) // 2
        canvas.paste(img_resized, (off_x, off_y))
        return canvas
    
    def _hw_display_image(self, img: Image.Image) -> None:
        """Persist image to a temp PNG and hand off to unified backend.

        We serialize to file because current backend APIs accept a path. If a
        future backend exposes a direct PIL interface we can branch on its
        capabilities, but this keeps things simple and consistent now.
        """
        from tempfile import NamedTemporaryFile
        tmp_path = None
        try:
            # Create temp file in cache_dir so our cleanup policy covers it
            with NamedTemporaryFile(delete=False, suffix=".png", dir=self.cache_dir) as f:
                img.save(f.name, format="PNG")
                tmp_path = f.name
            self.display_image(tmp_path)
        except Exception as e:  # noqa: BLE001
            self.logger.error("Hardware display failed: %s", e)
        finally:
            # Always attempt to remove the temp file we created for hardware handoff
            if tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
            # Also sweep other stale temp artifacts occasionally
            self._cleanup_cache_temps()

    def _cleanup_cache_temps(self, max_age_seconds: int = 600) -> None:
        """Remove stray temp artifacts in cache_dir older than max_age_seconds.

        Targets patterns left behind by crashes or abrupt restarts:
        - files ending with '.tmp'
        - files starting with 'tmp_'
        - legacy fixed temp names like 'tmp_display.png'/'default_resized.png' if stale
        """
        try:
            now = __import__("time").time()
            for name in os.listdir(self.cache_dir):
                if not (name.endswith(".tmp") or name.startswith("tmp_") or name in {"tmp_display.png", "default_resized.png"}):
                    continue
                path = os.path.join(self.cache_dir, name)
                try:
                    if not os.path.isfile(path):
                        continue
                    age = now - os.path.getmtime(path)
                    if age >= max_age_seconds:
                        os.remove(path)
                except Exception:
                    # Best-effort cleanup; ignore individual failures
                    pass
        except Exception:
            pass

    def _enforce_cache_retention(self, keep: int = 3) -> None:
        """Keep only the most recent 'keep' non-temp files in cache_dir.

        Non-temp means: not ending with '.tmp' and not starting with 'tmp_'. This
        targets our content cache regardless of file extension or lack thereof.
        """
        try:
            entries = []
            for name in os.listdir(self.cache_dir):
                if name.endswith('.tmp') or name.startswith('tmp_'):
                    continue
                path = os.path.join(self.cache_dir, name)
                if not os.path.isfile(path):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except Exception:
                    continue
                entries.append((mtime, path))

            # Newest first
            entries.sort(key=lambda t: t[0], reverse=True)
            for _mtime, path in entries[keep:]:
                try:
                    os.remove(path)
                except Exception:
                    # ignore individual failures
                    pass
        except Exception:
            # best effort; ignore top-level failures
            pass
