"""Simple caching mechanism for frequently accessed data."""
from __future__ import annotations

import time
from typing import Any, Optional
from threading import Lock
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    """Represents a cached item with expiration."""
    value: Any
    expires_at: float  # timestamp when entry expires
    hits: int = field(default=0)


class TTLCache:
    """Thread-safe TTL (Time To Live) cache."""
    
    def __init__(self, default_ttl: float = 300.0):  # 5 minutes default
        self._cache: dict[str, CacheEntry] = {}
        self._lock = Lock()
        self.default_ttl = default_ttl
        self._stats = {
            'hits': 0,
            'misses': 0,
            'sets': 0,
            'evictions': 0
        }
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if it exists and hasn't expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._stats['misses'] += 1
                return None
            
            if time.time() > entry.expires_at:
                # Expired
                del self._cache[key]
                self._stats['evictions'] += 1
                self._stats['misses'] += 1
                return None
            
            # Valid entry
            entry.hits += 1
            self._stats['hits'] += 1
            return entry.value
    
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value in cache with optional TTL."""
        if ttl is None:
            ttl = self.default_ttl
        
        expires_at = time.time() + ttl
        entry = CacheEntry(value=value, expires_at=expires_at)
        
        with self._lock:
            self._cache[key] = entry
            self._stats['sets'] += 1
    
    def delete(self, key: str) -> bool:
        """Delete key from cache. Returns True if key existed."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            # Reset stats but keep historical data for monitoring
            self._stats = {
                'hits': self._stats['hits'],
                'misses': self._stats['misses'],
                'sets': self._stats['sets'],
                'evictions': self._stats['evictions']
            }
    
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total_requests = self._stats['hits'] + self._stats['misses']
            hit_rate = (self._stats['hits'] / total_requests * 100) if total_requests > 0 else 0
            
            return {
                'size': len(self._cache),
                'hits': self._stats['hits'],
                'misses': self._stats['misses'],
                'sets': self._stats['sets'],
                'evictions': self._stats['evictions'],
                'hit_rate_percent': round(hit_rate, 2)
            }
    
    def cleanup_expired(self) -> int:
        """Remove expired entries and return count of removed items."""
        now = time.time()
        removed = 0
        
        with self._lock:
            expired_keys = [
                key for key, entry in self._cache.items()
                if now > entry.expires_at
            ]
            for key in expired_keys:
                del self._cache[key]
                removed += 1
            
            self._stats['evictions'] += removed
        
        return removed


# Global caches for different types of data
file_metadata_cache = TTLCache(default_ttl=60.0)  # File metadata expires after 1 minute
token_usage_cache = TTLCache(default_ttl=300.0)  # Token usage stats expires after 5 minutes


def get_file_metadata_cached(file_path: str) -> Optional[dict]:
    """Get file metadata with caching."""
    cache_key = f"file_meta:{file_path}"
    cached = file_metadata_cache.get(cache_key)
    if cached is not None:
        return cached
    
    # Not in cache, compute and store
    try:
        from pathlib import Path
        path = Path(file_path)
        if not path.exists():
            return None
            
        stat = path.stat()
        metadata = {
            'size': stat.st_size,
            'modified': stat.st_mtime,
            'is_file': path.is_file(),
            'is_dir': path.is_dir(),
            'exists': True
        }
        
        file_metadata_cache.set(cache_key, metadata)
        return metadata
    except Exception:
        return None


def invalidate_file_metadata(file_path: str) -> None:
    """Invalidate cached file metadata."""
    cache_key = f"file_meta:{file_path}"
    file_metadata_cache.delete(cache_key)


def get_token_usage_stats() -> dict:
    """Get token usage statistics with caching."""
    cache_key = "token_usage:stats"
    cached = token_usage_cache.get(cache_key)
    if cached is not None:
        return cached
    
    # In a real implementation, this would compute actual stats
    # For now, return placeholder
    stats = {
        'total_tokens': 0,
        'average_per_turn': 0,
        'cache_hits': 0
    }
    
    token_usage_cache.set(cache_key, stats, ttl=60.0)  # Shorter TTL for stats
    return stats