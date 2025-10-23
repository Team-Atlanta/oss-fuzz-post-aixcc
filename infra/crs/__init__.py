"""CRS (Compiler Repair System) package for OSS-Fuzz."""

from .crs_main import build_crs_impl, run_crs_impl

__all__ = ['build_crs_impl', 'run_crs_impl']
