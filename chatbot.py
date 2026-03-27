import json
import string
import random
import os
import nltk
import numpy as np
from nltk.stem import WordNetLemmatizer
import tensorflow as tf
from tensorflow.keras import Sequential
from tensorflow.keras.layers import Dense, Dropout, Input
import pickle

# Download required packages
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download("punkt")
try:
    nltk.data.find('corpora/wordnet')
except LookupError:
    nltk.download("wordnet")

lm = WordNetLemmatizer()

# Model paths
MODEL_DIR = os.path.join(os.path.dirname(__file__), 'models')
MODEL_PATH = os.path.join(MODEL_DIR, 'intent_model.h5')
VOCAB_PATH = os.path.join(MODEL_DIR, 'vocabulary.pkl')


class MedicalChatbot:
    """AI-driven medical chatbot using intent classification."""
    
    def __init__(self):
        self.model = None
        self.newWords = None
        self.ourClasses = None
        self.intents_data = None
        self.model_trained = False
        
    def load_dataset(self, dataset_path):
        """Load a dataset JSON file."""
        try:
            with open(dataset_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Dataset not found: {dataset_path}")
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in dataset: {dataset_path}")
    
    def prepare_training_data(self, data):
        """Prepare training data from intents."""
        self.ourClasses = []
        self.newWords = []
        documentX = []
        documentY = []
        
        # Extract words and classes
        for intent in data["ourIntents"]:
            for pattern in intent["patterns"]:
                ournewTkns = nltk.word_tokenize(pattern)
                self.newWords.extend(ournewTkns)
                documentX.append(pattern)
                documentY.append(intent['tag'])
            if intent["tag"] not in self.ourClasses:
                self.ourClasses.append(intent["tag"])
        
        # Lemmatize and clean
        self.newWords = [lm.lemmatize(word.lower()) for word in self.newWords 
                        if word not in string.punctuation]
        self.newWords = sorted(set(self.newWords))
        self.ourClasses = sorted(set(self.ourClasses))
        
        # Create training data
        trainingData = []
        outEmpty = [0] * len(self.ourClasses)
        
        for idx, doc in enumerate(documentX):
            bag0words = []
            text = lm.lemmatize(doc.lower())
            for word in self.newWords:
                bag0words.append(1) if word in text else bag0words.append(0)
            
            outputRow = list(outEmpty)
            outputRow[self.ourClasses.index(documentY[idx])] = 1
            trainingData.append([bag0words, outputRow])
        
        random.shuffle(trainingData)
        trainingData = np.array(trainingData, dtype=object)
        
        return (np.array(list(trainingData[:, 0])), 
                np.array(list(trainingData[:, 1])))
    
    def train(self, dataset_path, epochs=200):
        """Train the chatbot on a dataset."""
        print(f"Loading dataset from {dataset_path}...")
        self.intents_data = self.load_dataset(dataset_path)
        
        print("Preparing training data...")
        x, y = self.prepare_training_data(self.intents_data)
        
        print("Building neural network model...")
        self.model = Sequential()
        self.model.add(Input(shape=(len(x[0]),)))
        self.model.add(Dense(128, activation="relu"))
        self.model.add(Dropout(0.5))
        self.model.add(Dense(64, activation="relu"))
        self.model.add(Dropout(0.3))
        self.model.add(Dense(len(y[0]), activation='softmax'))
        
        optimizer = tf.keras.optimizers.Adam(learning_rate=0.01)
        self.model.compile(optimizer=optimizer, 
                          loss='categorical_crossentropy', 
                          metrics=['accuracy'])
        
        print(f"Training model for {epochs} epochs...")
        self.model.fit(x, y, epochs=epochs, verbose=1)
        self.model_trained = True
        print("✓ Model trained successfully!")
    
    def save_model(self):
        """Save trained model and vocabulary."""
        if not self.model_trained:
            raise ValueError("Model must be trained before saving!")
        
        os.makedirs(MODEL_DIR, exist_ok=True)
        
        print(f"Saving model to {MODEL_PATH}...")
        self.model.save(MODEL_PATH)
        
        print(f"Saving vocabulary to {VOCAB_PATH}...")
        vocab_data = {
            'newWords': self.newWords,
            'ourClasses': self.ourClasses,
            'intents': self.intents_data
        }
        with open(VOCAB_PATH, 'wb') as f:
            pickle.dump(vocab_data, f)
        
        print("✓ Model and vocabulary saved!")
    
    def load_trained_model(self):
        """Load a previously trained model."""
        if not os.path.exists(MODEL_PATH) or not os.path.exists(VOCAB_PATH):
            raise FileNotFoundError("Trained model files not found. Run training first!")
        
        print(f"Loading model from {MODEL_PATH}...")
        self.model = tf.keras.models.load_model(MODEL_PATH)
        
        print(f"Loading vocabulary from {VOCAB_PATH}...")
        with open(VOCAB_PATH, 'rb') as f:
            vocab_data = pickle.load(f)
        
        self.newWords = vocab_data['newWords']
        self.ourClasses = vocab_data['ourClasses']
        self.intents_data = vocab_data['intents']
        self.model_trained = True
        print("✓ Model loaded successfully!")
    
    def predict_intent(self, user_input, confidence_threshold=0.3):
        """
        Predict the intent of user input and return an appropriate response.
        Returns: (intent_tag, response, confidence)
        """
        if not self.model_trained:
            raise ValueError("Model not trained or loaded!")
        
        # Prepare input
        bag = []
        words = nltk.word_tokenize(user_input.lower())
        words = [lm.lemmatize(word) for word in words]
        
        for word in self.newWords:
            bag.append(1) if word in words else bag.append(0)
        
        # Predict
        prediction = self.model.predict(np.array([bag]), verbose=0)
        idx = np.argmax(prediction[0])
        confidence = float(prediction[0][idx])
        intent_tag = self.ourClasses[idx]
        
        # Get response if confidence is high enough
        if confidence >= confidence_threshold:
            for intent in self.intents_data["ourIntents"]:
                if intent["tag"] == intent_tag:
                    response = random.choice(intent.get("responses", 
                                                       ["I understand. Please continue."]))
                    return (intent_tag, response, confidence)
        
        # Low confidence fallback
        return (None, "I'm not sure I understood that. Could you rephrase?", confidence)
    
    def get_next_question(self, current_question_index=0):
        """
        Get the next question in sequence with its options.
        Returns: (question_text, responses_list, intent_tag) or None if no more questions
        """
        if not self.intents_data or not self.intents_data.get("ourIntents"):
            return None
        
        intents = self.intents_data["ourIntents"]
        
        if current_question_index >= len(intents):
            return None  # No more questions
        
        intent = intents[current_question_index]
        question = intent.get("patterns", [f"Question {current_question_index + 1}"])[0]
        responses = intent.get("responses", [])
        
        return (question, responses, intent["tag"])
    
    def process_response_and_get_next(self, user_response, current_question_index):
        """
        Process user response and return the next question.
        Returns: (acknowledgment, next_question, next_responses, next_intent, next_index)
        """
        # Acknowledge the response
        acknowledgment = f"Thank you for your response: '{user_response}'"
        
        # Get next question
        next_question_data = self.get_next_question(current_question_index + 1)
        
        if next_question_data:
            next_question, next_responses, next_intent = next_question_data
            return (acknowledgment, next_question, next_responses, next_intent, current_question_index + 1)
        else:
            # No more questions
            return (acknowledgment, "Thank you for completing the diagnostic questionnaire. A healthcare professional will review your responses.", [], "completed", -1)


# Global chatbot instance (lazy-loaded by API)
_chatbot_instance = None


def get_chatbot():
    """Get or create the global chatbot instance."""
    global _chatbot_instance
    if _chatbot_instance is None:
        _chatbot_instance = MedicalChatbot()
        try:
            _chatbot_instance.load_trained_model()
        except FileNotFoundError:
            print("Warning: No trained model found. You need to run train_model.py first.")
    return _chatbot_instance