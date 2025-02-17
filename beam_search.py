#encoding=utf-8
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""This file contains code to run beam search decoding"""
import copy
import tensorflow as tf
import numpy as np
import data
from collections import defaultdict, OrderedDict
FLAGS = tf.app.flags.FLAGS

class Hypothesis(object):
  """Class to represent a hypothesis during beam search. Holds all the information needed for the hypothesis."""

  def __init__(self, tokens, log_probs, state, attn_dists, p_gens, coverage,constraint,to_write,cur_index_cons,cur_index_word):
    """Hypothesis constructor.

    Args:
      tokens: List of integers. The ids of the tokens that form the summary so far.
      log_probs: List, same length as tokens, of floats, giving the log probabilities of the tokens so far.
      state: Current state of the decoder, a LSTMStateTuple.
      attn_dists: List, same length as tokens, of numpy arrays with shape (attn_length). These are the attention distributions so far.
      p_gens: List, same length as tokens, of floats, or None if not using pointer-generator model. The values of the generation probability so far.
      coverage: Numpy array of shape (attn_length), or None if not using coverage. The current coverage vector.
    """
    self.tokens = tokens
    self.log_probs = log_probs
    self.state = state
    self.attn_dists = attn_dists
    self.p_gens = p_gens
    self.coverage = coverage
    self.constraint = constraint
    self.to_write = to_write
    self.cur_index_cons = cur_index_cons
    self.cur_index_word = cur_index_word

  def extend(self, token, log_prob, state, attn_dist, p_gen, coverage,constraint,to_write,cur_index_cons,cur_index_word):
    """Return a NEW hypothesis, extended with the information from the latest step of beam search.

    Args:
      token: Integer. Latest token produced by beam search.
      log_prob: Float. Log prob of the latest token.
      state: Current decoder state, a LSTMStateTuple.
      attn_dist: Attention distribution from latest step. Numpy array shape (attn_length).
      p_gen: Generation probability on latest step. Float.
      coverage: Latest coverage vector. Numpy array shape (attn_length), or None if not using coverage.
    Returns:
      New Hypothesis for next step.
    """
    return Hypothesis(tokens = self.tokens + [token],
                      log_probs = self.log_probs + [log_prob],
                      state = state,
                      attn_dists = self.attn_dists + [attn_dist],
                      p_gens = self.p_gens + [p_gen],
                      coverage = coverage,
                      constraint=constraint,
                      to_write=to_write,
                      cur_index_cons=cur_index_cons,
                      cur_index_word=cur_index_word
                      )

  @property
  def latest_token(self):
    return self.tokens[-1]

  @property
  def log_prob(self):
    # the log probability of the hypothesis so far is the sum of the log probabilities of the tokens so far
    return sum(self.log_probs)

  @property
  def avg_log_prob(self):
    # normalize log probability by number of tokens (otherwise longer sequences always have lower probability)
    return self.log_prob / len(self.tokens)


def run_grid_beam_search(sess, model, vocab, batch, constraint):
    grid_constraint = constraint
    grid_height = sum([len(i) for i in grid_constraint])

    search_grid = OrderedDict()

    enc_states, dec_in_state = model.run_encoder(sess, batch)
    hyps = [Hypothesis(tokens=[vocab.word2id(data.START_DECODING)],
                       log_probs=[0.0],
                       state=dec_in_state,
                       attn_dists=[],
                       p_gens=[],
                       coverage=np.zeros([batch.enc_batch.shape[1]]),  # zero vector of length attention_length
                       constraint=grid_constraint,
                       to_write=list([k for k in range(len(grid_constraint))]),
                       cur_index_cons=-1,
                       cur_index_word=-1
                       ) for _ in xrange(FLAGS.beam_size)]
    search_grid[(0, 0)] = hyps
    results=[]

    for i in range(1,FLAGS.max_dec_steps):
        j_start = max(i - (FLAGS.max_dec_steps - grid_height), 0)
        j_end = min(i, grid_height) + 1
        for j in range(j_start,j_end):
            all_hyps = []
            if (i-1,j) in search_grid:
                latest_tokens = [h.latest_token for h in search_grid[(i-1,j)]]
                num_token = len(latest_tokens)
                while len(latest_tokens) < FLAGS.beam_size:
                    latest_tokens.append(latest_tokens[-1])
                latest_tokens = [t if t in xrange(vocab.size()) else vocab.word2id(data.UNKNOWN_TOKEN) for t in
                                 latest_tokens]
                states = [h.state for h in search_grid[(i-1,j)]]
                while len(states) < FLAGS.beam_size:
                    states.append(states[-1])
                prev_coverage = [h.coverage for h in search_grid[(i-1,j)]]
                while len(prev_coverage) < FLAGS.beam_size:
                    prev_coverage.append(prev_coverage[-1])

                (topk_ids, topk_log_probs, new_states, attn_dists, p_gens, new_coverage) = model.decode_onestep(
                    sess=sess,
                    batch=batch,
                    latest_tokens=latest_tokens,
                    enc_states=enc_states,
                    dec_init_states=states,
                    prev_coverage=prev_coverage)

                num_orig_hyps = 1 if i==1 else num_token

                for k in xrange(num_orig_hyps):
                    h, new_state, attn_dist, p_gen, new_coverage_i = search_grid[i-1,j][k], new_states[k], attn_dists[k], p_gens[
                        k], new_coverage[k]  # take the ith hypothesis and new decoder state info
                    if h.cur_index_word!=-1 or h.cur_index_cons!=-1:
                        continue
                    for l in xrange(FLAGS.beam_size * 2):  # for each of the top 2*beam_size hyps:
                        # Extend the ith hypothesis with the jth option
                        new_hyp = h.extend(token=topk_ids[k, l],
                                           log_prob=topk_log_probs[k, l],
                                           state=new_state,
                                           attn_dist=attn_dist,
                                           p_gen=p_gen,
                                           coverage=new_coverage_i,
                                           constraint=h.constraint,
                                           to_write=h.to_write,
                                           cur_index_cons=h.cur_index_cons,
                                           cur_index_word=h.cur_index_word
                                           )

                        all_hyps.append(new_hyp)
            if (i-1,j-1) in search_grid:
                tmp_hyp = get_generation_hyps(search_grid[(i - 1, j - 1)], batch, vocab, sess, model, enc_states)
                if len(tmp_hyp)>0:
                    all_hyps.extend(tmp_hyp)
                tmp_hyp = get_continue_hyps(search_grid[(i - 1, j - 1)], batch, vocab, sess, model, enc_states)
                if len(tmp_hyp)>0:
                    all_hyps.extend(tmp_hyp)

            # Filter and collect any hypotheses that have produced the end token.
            hyps = []  # will contain hypotheses for the next step
            for h in sort_hyps(all_hyps):  # in order of most likely h
                 if h.latest_token == vocab.word2id(data.STOP_DECODING):  # if stop token is reached...
                     # If this hypothesis is sufficiently long, put in results. Otherwise discard.
                     if i >= FLAGS.min_dec_steps and len(h.to_write)==0:
                         results.append(h)
                 else:  # hasn't reached stop token, so continue to extend this hypothesis
                     hyps.append(h)
                 if len(hyps) == FLAGS.beam_size:
                     # Once we've collected beam_size-many hypotheses for the next step, or beam_size-many complete hypotheses, stop.
                       break
            search_grid[(i, j)] = hyps
    if len(results) == 0:  # if we don't have any complete results, add all current hypotheses (incomplete summaries) to results
        results = hyps
    #final = sort_hyps(results)[0]
    final = sort_hyps(results)[1]
    return final


def get_generation_hyps(search_grid,batch,vocab,sess,model,enc_states):
        new_cons=[]
        latest_tokens = [h.latest_token for h in search_grid]  # latest token produced by each hypothesis
        num_token = len(latest_tokens)
        while len(latest_tokens) < FLAGS.beam_size:
            latest_tokens.append(latest_tokens[-1])
        latest_tokens = [t if t in xrange(vocab.size()) else vocab.word2id(data.UNKNOWN_TOKEN) for t in
                         latest_tokens]  # change any in-article temporary OOV ids to [UNK] id, so that we can lookup word embeddings
        states = [h.state for h in search_grid]  # list of current decoder states of the hypotheses
        while len(states) < FLAGS.beam_size:
            states.append(states[-1])
        prev_coverage = [h.coverage for h in search_grid]  # list of coverage vectors (or None)
        while len(prev_coverage) < FLAGS.beam_size:
            prev_coverage.append(prev_coverage[-1])

        # Run one step of the decoder to get the new info
        (topk_ids, topk_log_probs, new_states, attn_dists, p_gens, new_coverage) = model.decode_onestep(
            sess=sess,
            batch=batch,
            latest_tokens=latest_tokens,
            enc_states=enc_states,
            dec_init_states=states,
            prev_coverage=prev_coverage)
        for i in range(len(search_grid)):
            hy  = search_grid[i]
            topk_ids_i, topk_log_probs_i, new_states_i, attn_dists_i, p_gens_i, new_coverage_i = topk_ids[i], topk_log_probs[i], new_states[i], attn_dists[i], p_gens[i], new_coverage[i]
            if hy.cur_index_cons==-1 and hy.cur_index_word==-1:
                for j in range(len(hy.to_write)):
                    str_cons= hy.constraint[hy.to_write[j]][0]
                    index = np.where(topk_ids_i==str_cons)[0][0]
                    new_hy = hy.extend(token=topk_ids[i, index],
                                       log_prob=topk_log_probs[i, index],
                                       state=new_states_i,
                                       attn_dist=attn_dists_i,
                                       p_gen=p_gens_i,
                                       coverage=new_coverage_i,
                                       constraint=hy.constraint,
                                       to_write=hy.to_write,
                                       cur_index_cons=hy.cur_index_cons,
                                       cur_index_word=hy.cur_index_word
                                       )
                    new_hy_cp = copy.deepcopy(new_hy)
                    if len(new_hy_cp.constraint[new_hy_cp.to_write[j]]) == 1:
                        new_hy_cp.to_write.pop(j)
                        new_hy_cp.cur_index_word = -1
                    else:
                        new_hy_cp.cur_index_cons=j
                        new_hy_cp.cur_index_word=0
                    new_cons.append(new_hy_cp)
        return new_cons


def get_continue_hyps(search_grid, batch, vocab, sess, model, enc_states):
    new_cons = []

    latest_tokens = [h.latest_token for h in search_grid]  # latest token produced by each hypothesis
    num_token = len(latest_tokens)
    while len(latest_tokens) < FLAGS.beam_size:
        latest_tokens.append(latest_tokens[-1])
    latest_tokens = [t if t in xrange(vocab.size()) else vocab.word2id(data.UNKNOWN_TOKEN) for t in
                     latest_tokens]  # change any in-article temporary OOV ids to [UNK] id, so that we can lookup word embeddings
    states = [h.state for h in search_grid]  # list of current decoder states of the hypotheses
    while len(states) < FLAGS.beam_size:
        states.append(states[-1])
    prev_coverage = [h.coverage for h in search_grid]  # list of coverage vectors (or None)
    while len(prev_coverage) < FLAGS.beam_size:
        prev_coverage.append(prev_coverage[-1])

    # Run one step of the decoder to get the new info
    (topk_ids, topk_log_probs, new_states, attn_dists, p_gens, new_coverage) = model.decode_onestep(
        sess=sess,
        batch=batch,
        latest_tokens=latest_tokens,
        enc_states=enc_states,
        dec_init_states=states,
        prev_coverage=prev_coverage)
    for i in range(len(search_grid)):
        hy = search_grid[i]
        topk_ids_i, topk_log_probs_i, new_states_i, attn_dists_i, p_gens_i, new_coverage_i = topk_ids[i], \
                                                                                             topk_log_probs[i], \
                                                                                             new_states[i], attn_dists[
                                                                                                 i], p_gens[i], \
                                                                                             new_coverage[i]
        if hy.cur_index_cons!= -1 or hy.cur_index_word!= -1:

            str_cons = hy.constraint[hy.to_write[hy.cur_index_cons]][hy.cur_index_word+1]
            index = np.where(topk_ids_i == str_cons)[0][0]
            new_hy = hy.extend(token=topk_ids[i, index],
                               log_prob=topk_log_probs[i, index],
                               state=new_states_i,
                               attn_dist=attn_dists_i,
                               p_gen=p_gens_i,
                               coverage=new_coverage_i,
                               constraint=hy.constraint,
                               to_write=hy.to_write,
                               cur_index_cons=hy.cur_index_cons,
                               cur_index_word=hy.cur_index_word
                               )
            new_hy_cp = copy.deepcopy(new_hy)
            if new_hy_cp.cur_index_word+1 == len(new_hy_cp.constraint[new_hy_cp.to_write[new_hy_cp.cur_index_cons]])-1:
                new_hy_cp.to_write.pop(new_hy_cp.cur_index_cons)
                new_hy_cp.cur_index_cons = -1
                new_hy_cp.cur_index_word = -1
            else:
                new_hy_cp.cur_index_word = new_hy_cp.cur_index_word+1
            new_cons.append(new_hy_cp)
    return new_cons

def run_beam_search(sess, model, vocab, batch):
  """Performs beam search decoding on the given example.

  Args:
    sess: a tf.Session
    model: a seq2seq model
    vocab: Vocabulary object
    batch: Batch object that is the same example repeated across the batch

  Returns:
    best_hyp: Hypothesis object; the best hypothesis found by beam search.
  """
  # Run the encoder to get the encoder hidden states and decoder initial state
  enc_states, dec_in_state = model.run_encoder(sess, batch)
  # dec_in_state is a LSTMStateTuple
  # enc_states has shape [batch_size, <=max_enc_steps, 2*hidden_dim].

  # Initialize beam_size-many hyptheses
  hyps = [Hypothesis(tokens=[vocab.word2id(data.START_DECODING)],
                     log_probs=[0.0],
                     state=dec_in_state,
                     attn_dists=[],
                     p_gens=[],
                     coverage=np.zeros([batch.enc_batch.shape[1]]) # zero vector of length attention_length
                     ) for _ in xrange(FLAGS.beam_size)]
  results = [] # this will contain finished hypotheses (those that have emitted the [STOP] token)

  steps = 0
  while steps < FLAGS.max_dec_steps and len(results) < FLAGS.beam_size:
    latest_tokens = [h.latest_token for h in hyps] # latest token produced by each hypothesis
    latest_tokens = [t if t in xrange(vocab.size()) else vocab.word2id(data.UNKNOWN_TOKEN) for t in latest_tokens] # change any in-article temporary OOV ids to [UNK] id, so that we can lookup word embeddings
    states = [h.state for h in hyps] # list of current decoder states of the hypotheses
    prev_coverage = [h.coverage for h in hyps] # list of coverage vectors (or None)

    # Run one step of the decoder to get the new info
    (topk_ids, topk_log_probs, new_states, attn_dists, p_gens, new_coverage) = model.decode_onestep(sess=sess,
                        batch=batch,
                        latest_tokens=latest_tokens,
                        enc_states=enc_states,
                        dec_init_states=states,
                        prev_coverage=prev_coverage)

    # Extend each hypothesis and collect them all in all_hyps
    all_hyps = []
    num_orig_hyps = 1 if steps == 0 else len(hyps) # On the first step, we only had one original hypothesis (the initial hypothesis). On subsequent steps, all original hypotheses are distinct.
    for i in xrange(num_orig_hyps):
      h, new_state, attn_dist, p_gen, new_coverage_i = hyps[i], new_states[i], attn_dists[i], p_gens[i], new_coverage[i]  # take the ith hypothesis and new decoder state info
      for j in xrange(FLAGS.beam_size * 2):  # for each of the top 2*beam_size hyps:
        # Extend the ith hypothesis with the jth option
        new_hyp = h.extend(token=topk_ids[i, j],
                           log_prob=topk_log_probs[i, j],
                           state=new_state,
                           attn_dist=attn_dist,
                           p_gen=p_gen,
                           coverage=new_coverage_i)
        all_hyps.append(new_hyp)

    # Filter and collect any hypotheses that have produced the end token.
    hyps = [] # will contain hypotheses for the next step
    for h in sort_hyps(all_hyps): # in order of most likely h
      if h.latest_token == vocab.word2id(data.STOP_DECODING): # if stop token is reached...
        # If this hypothesis is sufficiently long, put in results. Otherwise discard.
        if steps >= FLAGS.min_dec_steps:
          results.append(h)
      else: # hasn't reached stop token, so continue to extend this hypothesis
        hyps.append(h)
      if len(hyps) == FLAGS.beam_size or len(results) == FLAGS.beam_size:
        # Once we've collected beam_size-many hypotheses for the next step, or beam_size-many complete hypotheses, stop.
        break

    steps += 1

  # At this point, either we've got beam_size results, or we've reached maximum decoder steps

  if len(results)==0: # if we don't have any complete results, add all current hypotheses (incomplete summaries) to results
    results = hyps

  # Sort hypotheses by average log probability
  hyps_sorted = sort_hyps(results)

  # Return the hypothesis with highest average log prob
  return hyps_sorted[0]

def sort_hyps(hyps):
  """Return a list of Hypothesis objects, sorted by descending average log probability"""
  return sorted(hyps, key=lambda h: h.avg_log_prob, reverse=True)
