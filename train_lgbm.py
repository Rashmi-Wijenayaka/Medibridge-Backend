#!/usr/bin/env python
"""
Pre-train LightGBM diagnosis models for all dataset areas.

Run once (or after updating datasets) from the backend/ directory:

    python train_lgbm.py

Models are saved to backend/models/lgbm_<area>.pkl
They are also auto-trained on first inference if missing.
"""

import os
import sys

# Ensure the api package is importable when running from backend/
sys.path.insert(0, os.path.dirname(__file__))

from api.lgbm_diagnosis import train_all_areas

if __name__ == '__main__':
    print('=' * 55)
    print('  LightGBM Diagnosis Model Training')
    print('=' * 55)
    train_all_areas()
    print('\nAll models saved to backend/models/')
    print('You can now run the Django server.')
