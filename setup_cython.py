#!/usr/bin/env python3
"""
Setup script for compiling Cython modules (FASE 8).

Usage:
    python setup_cython.py build_ext --inplace
"""

from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False
    print("Cython not available — skipping compilation. Bot will use Python fallback.")

if HAS_CYTHON:
    extensions = [
        Extension(
            "poly_data.book_cython",
            ["poly_data/book_cython.pyx"],
            extra_compile_args=["-O3", "-march=native"],
            extra_link_args=["-O3"],
        ),
    ]

    setup(
        name="gabagool_cython",
        ext_modules=cythonize(extensions, compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "initializedcheck": False,
        }),
        zip_safe=False,
    )
else:
    setup(name="gabagool_cython")
