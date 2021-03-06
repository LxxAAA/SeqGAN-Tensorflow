import numpy as np
import tensorflow as tf
from tensorflow.contrib import rnn, seq2seq, slim
from .common import ThresholdHelper, safe_log


class Generator(object):
    def __init__(
        self,
        batch_size,
        seq_len,
        vocab_size,
        emb_dim,
        hidden_dim,
        start_token,
        learning_rate=0.01,
        grad_clip=5.0
    ):
        '''
            please check SeqGAN.py for the definition of arguments
        '''
        self.seq_len = seq_len
        self.batch_size = batch_size

        given_tokens = tf.placeholder(
            tf.int32, shape=[batch_size, seq_len], name='given_tokens')
        start_tokens = tf.Variable(
            tf.tile([start_token], [batch_size]), name='start_tokens')

        with tf.variable_scope('generator'):
            RNN = rnn.LSTMCell(hidden_dim)
            embedding = tf.Variable(
                tf.random_normal([vocab_size, emb_dim], stddev=0.1))

            decision_W = tf.Variable(
                tf.random_normal([hidden_dim, vocab_size]), name='decision_W')
            decision_b = tf.Variable(tf.zeros([vocab_size]), name='decision_b')

            # At each step:
            #   pretrain: all tokens come from a given sequence
            #   generate: all tokens are sampled from the last output
            #   rollout: part of leading tokens come from a given sequence,
            #      and the rest ones are sampled
            # Therefore we define a helper that can:
            #   1. Translate discrete tokens into embeddings
            #   2. Take tokens from the given sequence before a threshold T,
            #      and sample tokens after T so that:
            #           pretrain setting equals to T==seq_len
            #           generate setting equals to T==0
            #           rollout setting equals to T==given_len
            #   3. Add a decision layer after RNN

            output_ids = []
            output_probs = []
            for i in range(seq_len+1):
                threshold_helper = ThresholdHelper(
                    threshold=i,
                    seq_len=seq_len,
                    embedding=embedding,
                    given_tokens=given_tokens,
                    start_tokens=start_tokens,
                    decision_variables=(decision_W, decision_b))
                decoder = seq2seq.BasicDecoder(
                    cell=RNN, helper=threshold_helper,
                    initial_state=RNN.zero_state(batch_size, 'float32'))
                final_outputs, final_state, final_sequence_lengths = \
                    seq2seq.dynamic_decode(
                        decoder=decoder, maximum_iterations=seq_len)

                output_ids.append(final_outputs.sample_id)
                output_probs.append(
                    tf.nn.softmax(
                        tf.tensordot(final_outputs.rnn_output,
                                     decision_W,
                                     axes=[[2], [0]]) +
                        decision_b[None, None, :]))
        self.output_ids = output_ids
        self.output_probs = output_probs
        self.given_tokens = given_tokens

        # pretrain
        logit = safe_log(self.output_probs[seq_len])
        pretrain_loss = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=given_tokens, logits=logit))
        pretrain_optimizer = tf.train.AdamOptimizer(
            learning_rate=learning_rate)
        pretrain_op = slim.learning.create_train_op(
            pretrain_loss, pretrain_optimizer, clip_gradient_norm=5.0)

        self.pretrain_loss = pretrain_loss
        self.pretrain_op = pretrain_op
        self.pretrain_summary = tf.summary.scalar(
            "g_pretrain_loss", pretrain_loss)

        # RL
        rewards = tf.placeholder(tf.float32,
                                 shape=[batch_size, seq_len],
                                 name="rewards")
        g_seq = self.output_ids[seq_len]  # follow the generated one
        g_prob = self.output_probs[seq_len]
        g_loss = -tf.reduce_mean(
            tf.reduce_sum(tf.one_hot(g_seq, vocab_size) * safe_log(g_prob), -1) *
            rewards
        )
        g_optimizer = tf.train.AdamOptimizer(
            learning_rate=learning_rate)
        g_op = slim.learning.create_train_op(
            g_loss, g_optimizer, clip_gradient_norm=5.0)
        g_summary = tf.summary.merge([
            tf.summary.scalar("g_loss", g_loss),
            tf.summary.scalar("g_reward", tf.reduce_mean(rewards))
        ])

        self.rewards = rewards
        self.g_op = g_op
        self.g_summary = g_summary
        self.image_summary = tf.summary.merge([
            tf.summary.image(
                "real_samples",
                tf.expand_dims(tf.one_hot(given_tokens, vocab_size), -1)
            ),
            tf.summary.image(
                "fake_samples",
                tf.expand_dims(tf.one_hot(output_ids[0], vocab_size), -1)
            ),
        ])

    def generate(self, sess):
        return sess.run(self.output_ids[0])

    def rollout(self, sess, given_tokens, keep_steps=0, with_probs=False):
        feed_dict = {self.given_tokens: given_tokens}
        if with_probs:
            output_tensors = [self.output_ids[keep_steps],
                              self.output_probs[keep_steps]]
        else:
            output_tensors = self.output_ids[keep_steps]
        return sess.run(output_tensors, feed_dict=feed_dict)

    def pretrain(self, sess, given_tokens):
        feed_dict = {self.given_tokens: given_tokens}
        _, summary = sess.run([self.pretrain_op, self.pretrain_summary],
                              feed_dict=feed_dict)
        return summary

    def train(self, sess, given_tokens, rewards):
        feed_dict = {self.given_tokens: given_tokens,
                     self.rewards: rewards}
        _, summary = sess.run([self.g_op, self.g_summary],
                              feed_dict=feed_dict)
        return summary

    def get_reward(self, sess, given_tokens, rollout_num, discriminator):
        rewards = np.zeros((self.batch_size, self.seq_len))
        for keep_num in range(1, self.seq_len):
            for i in range(rollout_num):
                # Markov Chain Sample
                mc_sample = self.rollout(
                    sess, given_tokens, keep_steps=keep_num)
                rewards[:, keep_num] += discriminator.\
                    get_truth_prob(sess, mc_sample)
        rewards /= rollout_num
        return rewards
