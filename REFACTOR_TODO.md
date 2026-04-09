# mimir-display Refactor TODO

Tracked items from the April 2026 code review. Work top-down — earlier items unblock or simplify later ones.

---

## 1. Delete `ContentDownloader.process_assignment()` ✅
**File:** `mimir_display/content/downloader.py` ~309–335  
**Risk:** None — method is never called. All processing goes through `AssignmentProcessor.process_assignment()`.  
**Action:** Delete the method body. Verify no callers exist with a codebase grep.

---

## 2. Fix `ensure_future` → `create_task` with error logging ✅
**File:** `mimir_display/mqtt_client_manager.py` ~479  
**Risk:** Low — existing behavior is fire-and-forget; adding error logging is strictly additive.  
**Action:** Replace `asyncio.ensure_future(self._provision_self_register())` with
`asyncio.create_task(...)` and attach a done-callback that logs any exception.

---

## 3. Consolidate directory resolution to `resolve_writable_dir` ✅
**Files:**
- `mimir_display/storage/registration.py` ~45–91 (own implementation)
- `mimir_display/storage/device_config.py` ~39–51 (own implementation)
- `mimir_display/content/downloader.py` ~40–74 (own implementation)
- `mimir_display/utils/helpers.py` ~147–196 (canonical implementation)

**Risk:** Medium — change affects storage path resolution at startup. Test on device after.  
**Action:** Replace the three custom implementations with calls to `resolve_writable_dir`.

---

## 4. Extract `.local` hostname fallback to a shared utility ✅
**Files:**
- `mimir_display/network/mqtt/commands.py` ~605–620
- `mimir_display/content/downloader.py` ~176–189

**Risk:** Low — pure extraction, no logic change.  
**Action:** Add `resolve_local_url(url: str) -> str` to `utils/helpers.py` and call it from both sites.

---

## 5. Fix double logger init in `MqttDisplayClientManager.__init__` ✅
**File:** `mimir_display/mqtt_client_manager.py` ~66  
**Risk:** None — the conditional is always false; removing it changes nothing at runtime.  
**Action:** Remove the `if not hasattr(self, 'logger') else self.logger` guard; keep the plain assignment.

---

## 6. Fix `shutdown()` race condition ✅
**File:** `mimir_display/mqtt_client_manager.py` ~703  
**Risk:** Low — race is unlikely in practice but easy to fix correctly.  
**Action:** Replace boolean `_shutting_down` flag with `asyncio.Lock` so concurrent callers can't both pass the guard.

---

## 7. Fix `_has_valid_mqtt_host()` blocking `localhost` ✅
**File:** `mimir_display/mqtt_client_manager.py` ~563  
**Risk:** Low — dev/single-machine deployments may be broken today.  
**Action:** Add a config flag (e.g. `ALLOW_LOCAL_MQTT=true`) to bypass the block, and document the check.

---

## 8. Split `MqttCommandHandler` into domain-focused handlers
**File:** `mimir_display/network/mqtt/commands.py` (~680 lines, 10+ command types)  
**Risk:** Medium — central dispatch wiring needs updating.  
**Proposed split:**
- `RegistrationCommandHandler` — register, ready, registration_complete, finalize_registration, update_client
- `DisplayCommandHandler` — display_image, set_scene, clear_scene, refresh, assign

---

## 9. Split `MqttDisplayClientManager` into focused classes
**File:** `mimir_display/mqtt_client_manager.py` (~760 lines)  
**Risk:** High — touches the main execution path. Do last, after smaller items reduce surface area.  
**Proposed split:**
- `BootstrapManager` — platform API config fetch, MQTT broker setup, retry/polling
- `SplashRenderer` — building and displaying splash/pairing/provisioning screens
- `MqttDisplayClientManager` — thin orchestrator only

---

## 10. Standardize logging format and type hints
**Risk:** None — cosmetic only.  
**Action:**
- Pick `%`-style logging throughout (lazy evaluation, no f-strings in log calls)
- Pick `dict[str, Any]` (3.10+ builtins) or `Dict[str, Any]` (typing module) and apply consistently
