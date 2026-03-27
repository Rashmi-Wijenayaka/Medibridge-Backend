#!/usr/bin/env python
"""
Training script for the medical chatbot.
Run this once to train the model on all available medical datasets.

Usage:
    python train_model.py
"""

import os
import sys
from chatbot import MedicalChatbot

def main():
    """Train the chatbot on available datasets."""
    
    datasets_dir = os.path.join(os.path.dirname(__file__), 'Datasets')
    
    # We'll train on Head.json as the primary dataset
    # (In production, you might want to merge all datasets or train on a combined file)
    dataset_path = os.path.join(datasets_dir, 'Head.json')
    
    if not os.path.exists(dataset_path):
        print(f"Error: {dataset_path} not found!")
        print(f"Please ensure dataset files exist in: {datasets_dir}")
        sys.exit(1)
    
    print("=" * 60)
    print("  MEDICAL CHATBOT MODEL TRAINING")
    print("=" * 60)
    print(f"\nDataset: {dataset_path}")
    print("\nThis will train a neural network on medical intents.\n")
    
    # Create chatbot instance and train
    chatbot = MedicalChatbot()
    
    try:
        chatbot.train(dataset_path, epochs=200)
        chatbot.save_model()
        print("\n" + "=" * 60)
        print("✓ Training complete! Model saved successfully.")
        print("=" * 60)
        print("\nYou can now start the Django server and use the API.")
        print("The chatbot will use the trained model for intelligent responses.\n")
        
    except Exception as e:
        print(f"\nError during training: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
