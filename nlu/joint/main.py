import random
import sys
import os
import json
import time
import spacy
import tensorflow as tf
import numpy as np
from sklearn.metrics import accuracy_score

from . import data
from .model import Model
from . import metrics

# embedding size for labels
embedding_size = 64
# size of LSTM cells
hidden_size = 100
# size of batch
batch_size = 16
# number of training epochs
epoch_num = 50

MY_PATH = os.path.dirname(os.path.abspath(__file__))

DATASET = os.environ.get('DATASET', 'atis')
OUTPUT_FOLDER = os.environ.get('OUTPUT_FOLDER', 'last')
MODE = os.environ.get('MODE', None)
if not MODE:
    # for those two datasets, default to train full
    if DATASET == 'wit_en' or DATASET == 'wit_it':
        MODE = 'runtime'
    else:
        MODE = 'measures'
# the type of recurrent unit on the multi-turn: rnn or CRF
RECURRENT_MULTITURN=os.environ.get('RECURRENT_MULTITURN','gru')

# set this to 'no_all', 'no_bot_turn', 'no_previous_intent' for a partial single-turned net on multi-turn datasets
FORCE_SINGLE_TURN = os.environ.get('FORCE_SINGLE_TURN', False)
if FORCE_SINGLE_TURN:
    OUTPUT_FOLDER += '_single_' + FORCE_SINGLE_TURN
if RECURRENT_MULTITURN != 'gru':
    OUTPUT_FOLDER += '_' + RECURRENT_MULTITURN
if MODE=='measures':
    # don't overwrite anything
    #OUTPUT_FOLDER += str(time.time())
    pass

WORD_EMBEDDINGS = os.environ.get('WORD_EMBEDDINGS', 'large')
OUTPUT_FOLDER += '_we_' + WORD_EMBEDDINGS
RECURRENT_CELL = os.environ.get('RECURRENT_CELL', 'lstm')
OUTPUT_FOLDER += '_recurrent_cell_' + RECURRENT_CELL
ATTENTION = os.environ.get('ATTENTION', 'slots') # intents, slots, both, none
OUTPUT_FOLDER += '_attention_' + ATTENTION

print('environment variables:')
print('DATASET:', DATASET, '\nOUTPUT_FOLDER:', OUTPUT_FOLDER, '\nMODE:', MODE, '\nRECURRENT_MULTITURN:', RECURRENT_MULTITURN, '\nFORCE_SINGLE_TURN:', FORCE_SINGLE_TURN, '\nWORD_EMBEDDINGS:', WORD_EMBEDDINGS, '\nRECURRENT_CELL:', RECURRENT_CELL, '\nATTENTION:', ATTENTION)

def get_model(vocabs, tokenizer, language, multi_turn, input_steps, nlp):
    model = Model(input_steps, embedding_size, hidden_size, vocabs, WORD_EMBEDDINGS, RECURRENT_CELL, ATTENTION, multi_turn, None, RECURRENT_MULTITURN)
    model.build(nlp, tokenizer, language)
    return model


def train(mode):
    # maximum length of sentences
    input_steps = 50
    # load the train and dev datasets
    # TODO do cross validation
    folds = data.load_data(DATASET, mode)
    # fix the random seeds
    random_seed_init(len(folds[0]['data']))
    # preprocess them to list of training/test samples
    # a sample is made up of a tuple that contains
    # - an input sentence (list of words --> strings, padded)
    # - the real length of the sentence (int) to be able to recognize padding
    # - an output sequence (list of IOB annotations --> strings, padded)
    # - an output intent (string)
    multi_turn = folds[0]['meta'].get('multi_turn', False)
    print('multi_turn:', multi_turn)
    if multi_turn:
        input_steps *=2
        folds = [data.collapse_multi_turn_sessions(fold, FORCE_SINGLE_TURN) for fold in folds]
    folds = [data.adjust_sequences(fold, input_steps) for fold in folds]

    all_samples = [s for fold in folds for s in fold['data']] 
    meta_data = folds[0]['meta']

    
    # turn off multi_turn for the required additional feeds and previous intent RNN
    if multi_turn and FORCE_SINGLE_TURN == 'no_all' or FORCE_SINGLE_TURN == 'no_previous_intent':
        multi_turn = False
    # get the vocabularies for input, slot and intent
    vocabs = data.get_vocabularies(all_samples, meta_data)
    # and get the model
    if FORCE_SINGLE_TURN == 'no_previous_intent':
        # changing this now, implies that the model doesn't have previous intent
        multi_turn = False
    
    language_model_name = data.get_language_model_name(meta_data['language'], WORD_EMBEDDINGS)
    nlp = spacy.load(language_model_name)
    
    # initialize the history that will collect some measures
    history = {
        'intent_f1': np.zeros((epoch_num)),
        'slot_sequence_f1': np.zeros((epoch_num)),
        #'intent_accuracy': np.zeros((epoch_num)), # accuracy on single-label classification tasks is the same as micro-f1
        'slots_f1': np.zeros((epoch_num)), # value+role+entity comparison
        'slots_f1_cond': np.zeros((epoch_num)) # value+role+entity comparison conditioned to correct intent
    }

    for fold_number in range(0, len(folds)):
        # reset the graph for next iteration
        tf.reset_default_graph()
    
        training_samples = [s for (count,fold) in enumerate(folds) if count != fold_number for s in fold['data']]
        test_samples = folds[fold_number]['data']
        print('train samples', len(training_samples))
        if test_samples:
            print('test samples', len(test_samples))


        model = get_model(vocabs, meta_data['tokenizer'], meta_data['language'], multi_turn, input_steps, nlp)
        
        global_init_op = tf.global_variables_initializer()
        table_init_op = tf.tables_initializer()
        saver = tf.train.Saver()
        sess = tf.Session()
        
        # initialize the required parameters
        sess.run(global_init_op)
        sess.run(table_init_op)

        if multi_turn:
            print('i am multi turn')
        for epoch in range(epoch_num):
            mean_loss = 0.0
            train_loss = 0.0
            for i, batch in enumerate(data.get_batch(batch_size, training_samples)):
                # perform a batch of training
                _, loss, decoder_prediction, intent, mask = model.step(sess, "train", batch)
                mean_loss += loss
                train_loss += loss
                if i % 10 == 0:
                    if i > 0:
                        mean_loss = mean_loss / 10.0
                    #print('Average train loss at epoch %d, step %d: %f' % (epoch, i, mean_loss))
                    print('.', end='')
                    sys.stdout.flush()
                    mean_loss = 0
            train_loss /= (i + 1)
            print("[Epoch {}] Average train loss: {}".format(epoch, train_loss))

            if test_samples:
                # test each epoch once
                pred_iob = []
                pred_intents = []
                true_intents = []
                previous_intents = []
                for j, batch in enumerate(data.get_batch(batch_size, test_samples)):
                    decoder_prediction, intent = model.step(sess, "test", batch)
                    # from time-major matrix to sample-major
                    decoder_prediction = np.transpose(decoder_prediction, [1, 0])
                    if j == 0:
                        index = random.choice(range(len(batch)))
                        # index = 0
                        print("Input Sentence        : ", batch[index]['words'][:batch[index]['length']])
                        print("Slot Truth            : ", batch[index]['slots'][:batch[index]['length']])
                        print("Slot Prediction       : ", decoder_prediction[index][:batch[index]['length']].tolist())
                        print("Intent Truth          : ", batch[index]['intent'])
                        print("Intent Prediction     : ", intent[index])
                    slot_pred_length = list(np.shape(decoder_prediction))[1]
                    pred_padded = np.lib.pad(decoder_prediction, ((0, 0), (0, input_steps-slot_pred_length)),
                                            mode="constant", constant_values=0)
                    pred_iob.append(pred_padded)
                    # pred_padded is an array of shape (batch_size, pad_length)
                    #print(pred_padded)
                    #print("pred_intents", pred_intents, "intent", intent)
                    pred_intents.extend(intent)
                    true_intent = [sample['intent'] for sample in batch]
                    true_intents.extend(true_intent)
                    #print("true_intents", true_intents)
                    # print("slot_pred_length: ", slot_pred_length)
                    true_slot = np.array([sample['slots'] for sample in batch])
                    true_length = np.array([sample['length'] for sample in batch])
                    true_slot = true_slot[:, :slot_pred_length]
                    # print(np.shape(true_slot), np.shape(decoder_prediction))
                    # print(true_slot, decoder_prediction)
                    slot_acc = metrics.accuracy_score(true_slot, decoder_prediction, true_length)
                    intent_acc = metrics.accuracy_score(true_intent, intent)
                    print('.', end='')
                    sys.stdout.flush()
                    if multi_turn:
                        previous_intents.extend([sample['previous_intent'] for sample in batch])
                    #print("slot accuracy: {}, intent accuracy: {}".format(slot_acc, intent_acc))
                pred_iob_a = np.vstack(pred_iob)
                # pred_iob_a is of shape (n_test_samples, sequence_len)
                #print("pred_iob_a: ", pred_iob_a.shape)
                true_slots_iob = np.array([sample['slots'] for sample in test_samples])[:pred_iob_a.shape[0]]
                f1_intents = metrics.f1_for_intents(true_intents, pred_intents)
                #accuracy_intents = accuracy_score(true_intents, pred_intents)
                f1_slots_iob = metrics.f1_for_sequence_batch(true_slots_iob, pred_iob_a)
                # convert IOB to slots stringified LABEL:START_IDX-END_IDX for comparison
                #print(pred_iob_a)
                true_slots = data.sequence_iob_to_ents(true_slots_iob)
                pred_slots = data.sequence_iob_to_ents(pred_iob_a)
                #print(true_slots)
                #print(pred_slots)
                f1_slots = metrics.f1_slots(true_slots, pred_slots)
                f1_slots_cond = metrics.f1_slots_conditioned_intent(true_slots, pred_slots, true_intents, pred_intents)
                # print("true_slots_iob: ", true_slots_iob.shape)
                print('epoch {} ended'.format(epoch))
                print("F1 score SEQUENCE for epoch {}: {}".format(epoch, f1_slots_iob))
                print("F1 score INTENTS for epoch {}: {}".format(epoch, f1_intents))
                print("F1 SLOTS for epoch {}: {}".format(epoch, f1_slots))
                print("F1 SLOTS COND for epoch {}: {}".format(epoch, f1_slots_cond))
                history['intent_f1'][epoch] += f1_intents
                history['slot_sequence_f1'][epoch] += f1_slots_iob
                history['slots_f1'][epoch] += f1_slots
                history['slots_f1_cond'][epoch] += f1_slots_cond

                if multi_turn:
                    # evaluate the intent transitions in samples and the transition inferred
                    true_intent_changes, pred_intent_changes = list(zip(*[(prev != true, prev != pred) for prev, pred, true in zip(previous_intents, pred_intents, true_intents)]))
                    #f1_changes = metrics.f1_for_intents(true_intent_changes, pred_intent_changes)
                    true_positives, true_negatives = list(zip(*[(true and pred, not true and not pred) for true, pred in zip(true_intent_changes, pred_intent_changes)]))
                    #print("F1 score INTENT CHANGE for epoch {}: {} with {} true positives and {} true negatives over {} samples".format(epoch, f1_changes, sum(true_positives), sum(true_negatives), len(true_positives)))
                    print("INTENT CHANGE statistics for epoch {}: {} true positives and {} true negatives over {} samples".format(epoch, sum(true_positives), sum(true_negatives), len(true_positives)))

        # the iteration on the fold has completed

    # normalize scores of f1
    for k,values in history.items():
        values /= len(folds)
        print('averages over the K folds have been computed')

    real_folder = MY_PATH + '/results/' + OUTPUT_FOLDER + '/' + DATASET + '/'
    if not os.path.exists(real_folder):
        os.makedirs(real_folder)
    
    if test_samples:
        metrics.plot_f1_history(real_folder + 'f1.png', history)
        save_history(history, real_folder + 'history.json')
    else:
        saver = tf.train.Saver()
        saver.save(sess, real_folder + 'model.ckpt')



def random_seed_init(seed):
    random.seed(seed)
    tf.set_random_seed(seed)

def save_history(history, file_path):
    history_serializable = {k:v.tolist() for k,v in history.items()}
    with open(file_path, 'w') as out_file:
        json.dump(history_serializable, out_file)

if __name__ == '__main__':
    train(MODE)