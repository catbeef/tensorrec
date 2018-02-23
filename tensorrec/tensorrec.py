import logging
import numpy as np
import pickle
from scipy import sparse as sp
import six
import tensorflow as tf

from .loss_graphs import rmse_loss
from .recommendation_graphs import (project_biases, prediction_serial, split_sparse_tensor_indices,
                                    bias_prediction_dense, bias_prediction_serial, rank_predictions)
from .representation_graphs import linear_representation_graph
from .session_management import get_session


class TensorRec(object):

    def __init__(self, n_components=100,
                 user_repr_graph=linear_representation_graph,
                 item_repr_graph=linear_representation_graph,
                 loss_graph=rmse_loss,
                 biased=True):
        """
        A TensorRec recommendation model.
        :param n_components: Integer
        The dimension of a single output of the representation function. Must be >= 1.
        :param user_repr_graph: Method
        A method which creates TensorFlow nodes to calculate the user representation.
        See tensorrec.representation_graphs for examples.
        :param item_repr_graph: Method
        A method which creates TensorFlow nodes to calculate the item representation.
        See tensorrec.representation_graphs for examples.
        :param loss_graph: Method
        A method which creates TensorFlow nodes to calculate the loss function.
        See tensorrec.loss_graphs for examples.
        :param biased: Boolean
        If True, a bias value will be calculated for every user feature and item feature.
        """

        # Arg-check
        if (n_components is None) or (user_repr_graph is None) or (item_repr_graph is None) or (loss_graph is None):
            raise ValueError("All arguments to TensorRec() must be non-None")
        if n_components < 1:
            raise ValueError("n_components must be >= 1")

        self.n_components = n_components
        self.user_repr_graph_factory = user_repr_graph
        self.item_repr_graph_factory = item_repr_graph
        self.loss_graph_factory = loss_graph
        self.biased = biased

        # A list of the attr names of every graph hook attr
        self.graph_tensor_hook_attr_names = [

            # Top-level API nodes
            'tf_user_representation', 'tf_item_representation', 'tf_prediction_serial', 'tf_prediction', 'tf_rankings',

            # Training nodes
            'tf_basic_loss', 'tf_weight_reg_loss', 'tf_loss',

            # Feed placeholders
            'tf_n_users', 'tf_n_items', 'tf_user_feature_indices', 'tf_user_feature_values', 'tf_item_feature_indices',
            'tf_item_feature_values', 'tf_interaction_indices', 'tf_interaction_values', 'tf_learning_rate', 'tf_alpha',
        ]
        self.graph_operation_hook_attr_names = [
            # AdamOptimizer
            'tf_optimizer',
        ]
        self._clear_graph_hook_attrs()

        # A map of every graph hook attr name to the node name after construction
        # Tensors and operations are stored separated because they are handled differently by TensorFlow
        self.graph_tensor_hook_node_names = {}
        self.graph_operation_hook_node_names = {}

    def _clear_graph_hook_attrs(self):
        for graph_tensor_hook_attr_name in self.graph_tensor_hook_attr_names:
            self.__setattr__(graph_tensor_hook_attr_name, None)
        for graph_operation_hook_attr_name in self.graph_operation_hook_attr_names:
            self.__setattr__(graph_operation_hook_attr_name, None)

    def _attach_graph_hook_attrs(self):
        session = get_session()

        for graph_tensor_hook_attr_name in self.graph_tensor_hook_attr_names:
            graph_tensor_hook_node_name = self.graph_tensor_hook_node_names[graph_tensor_hook_attr_name]
            node = session.graph.get_tensor_by_name(name=graph_tensor_hook_node_name)
            self.__setattr__(graph_tensor_hook_attr_name, node)

        for graph_operation_hook_attr_name in self.graph_operation_hook_attr_names:
            graph_operation_hook_node_name = self.graph_operation_hook_node_names[graph_operation_hook_attr_name]
            node = session.graph.get_operation_by_name(name=graph_operation_hook_node_name)
            self.__setattr__(graph_operation_hook_attr_name, node)

    def _create_feed_dict(self, interactions_matrix, user_features_matrix, item_features_matrix,
                          extra_feed_kwargs=None):

        # Check that input data is of a sparse type
        if (interactions_matrix is not None) and (not sp.issparse(interactions_matrix)):
            raise Exception('Interactions must be a scipy sparse matrix')
        if not sp.issparse(user_features_matrix):
            raise Exception('User features must be a scipy sparse matrix')
        if not sp.issparse(item_features_matrix):
            raise Exception('Item features must be a scipy sparse matrix')

        # Create placeholders if interactions_matrix is none
        # TODO JK - This probably isn't necessary -- will the graph execute without it?
        if interactions_matrix is None:
            interactions_matrix = np.ones((user_features_matrix.shape[0], item_features_matrix.shape[0]))

        n_users, user_feature_indices, user_feature_values = self._process_matrix(user_features_matrix)
        n_items, item_feature_indices, item_feature_values = self._process_matrix(item_features_matrix)
        _, interaction_indices, interaction_values = self._process_matrix(interactions_matrix)

        feed_dict = {self.tf_n_users: n_users,
                     self.tf_n_items: n_items,
                     self.tf_user_feature_indices: user_feature_indices,
                     self.tf_user_feature_values: user_feature_values,
                     self.tf_item_feature_indices: item_feature_indices,
                     self.tf_item_feature_values: item_feature_values,
                     self.tf_interaction_indices: interaction_indices,
                     self.tf_interaction_values: interaction_values}

        if extra_feed_kwargs:
            feed_dict.update(extra_feed_kwargs)

        return feed_dict

    def _create_user_feed_dict(self, user_features_matrix, extra_feed_kwargs=None):

        if not sp.issparse(user_features_matrix):
            raise Exception('User features must be a scipy sparse matrix')

        n_users, user_feature_indices, user_feature_values = self._process_matrix(user_features_matrix)

        feed_dict = {self.tf_n_users: n_users,
                     self.tf_user_feature_indices: user_feature_indices,
                     self.tf_user_feature_values: user_feature_values}

        if extra_feed_kwargs:
            feed_dict.update(extra_feed_kwargs)

        return feed_dict

    def _create_item_feed_dict(self, item_features_matrix, extra_feed_kwargs=None):

        if not sp.issparse(item_features_matrix):
            raise Exception('Item features must be a scipy sparse matrix')

        n_items, item_feature_indices, item_feature_values = self._process_matrix(item_features_matrix)

        feed_dict = {self.tf_n_items: n_items,
                     self.tf_item_feature_indices: item_feature_indices,
                     self.tf_item_feature_values: item_feature_values}

        if extra_feed_kwargs:
            feed_dict.update(extra_feed_kwargs)

        return feed_dict

    def _process_matrix(self, features_matrix):

        if not isinstance(features_matrix, sp.coo_matrix):
            features_matrix = sp.coo_matrix(features_matrix)

        # "Actors" is used to signify "users or items" -- unused for interactions
        n_actors = features_matrix.shape[0]
        feature_indices = [pair for pair in six.moves.zip(features_matrix.row, features_matrix.col)]
        feature_values = features_matrix.data

        return n_actors, feature_indices, feature_values

    def _build_tf_graph(self, n_user_features, n_item_features):

        # Initialize placeholder values for inputs
        self.tf_n_users = tf.placeholder('int64')
        self.tf_n_items = tf.placeholder('int64')
        self.tf_user_feature_indices = tf.placeholder('int64', [None, 2])
        self.tf_user_feature_values = tf.placeholder('float', [None])
        self.tf_item_feature_indices = tf.placeholder('int64', [None, 2])
        self.tf_item_feature_values = tf.placeholder('float', [None])
        self.tf_interaction_indices = tf.placeholder('int64', [None, 2])
        self.tf_interaction_values = tf.placeholder('float', [None])
        self.tf_learning_rate = tf.placeholder('float', None)
        self.tf_alpha = tf.placeholder('float', None)

        # Construct the features and interactions as sparse matrices
        tf_user_features = tf.SparseTensor(self.tf_user_feature_indices, self.tf_user_feature_values,
                                           [self.tf_n_users, n_user_features])
        tf_item_features = tf.SparseTensor(self.tf_item_feature_indices, self.tf_item_feature_values,
                                           [self.tf_n_items, n_item_features])
        tf_interactions = tf.SparseTensor(self.tf_interaction_indices, self.tf_interaction_values,
                                          [self.tf_n_users, self.tf_n_items])

        # Build the representations
        self.tf_user_representation, user_weights = \
            self.user_repr_graph_factory(tf_features=tf_user_features,
                                         n_components=self.n_components,
                                         n_features=n_user_features,
                                         node_name_ending='user')
        self.tf_item_representation, item_weights = \
            self.item_repr_graph_factory(tf_features=tf_item_features,
                                         n_components=self.n_components,
                                         n_features=n_item_features,
                                         node_name_ending='item')

        # Collect the weights for normalization
        tf_weights = []
        tf_weights.extend(user_weights)
        tf_weights.extend(item_weights)

        # Prediction = user_repr * item_repr + user_bias + item_bias
        # For the parallel prediction case, repr matrices can be multiplied together and the projected biases can be
        # broadcast across the resultant matrix
        self.tf_prediction = tf.matmul(self.tf_user_representation, self.tf_item_representation, transpose_b=True)

        tf_x_user, tf_x_item = split_sparse_tensor_indices(tf_sparse_tensor=tf_interactions, n_dimensions=2)
        self.tf_prediction_serial = prediction_serial(tf_user_representation=self.tf_user_representation,
                                                      tf_item_representation=self.tf_item_representation,
                                                      tf_x_user=tf_x_user,
                                                      tf_x_item=tf_x_item)

        # Add biases, if this is a biased estimator
        if self.biased:
            tf_user_feature_biases, tf_projected_user_biases = project_biases(
                tf_features=tf_user_features, n_features=n_user_features
            )
            tf_item_feature_biases, tf_projected_item_biases = project_biases(
                tf_features=tf_item_features, n_features=n_item_features
            )

            tf_weights.append(tf_user_feature_biases)
            tf_weights.append(tf_item_feature_biases)

            self.tf_prediction = bias_prediction_dense(tf_prediction=self.tf_prediction,
                                                       tf_projected_user_biases=tf_projected_user_biases,
                                                       tf_projected_item_biases=tf_projected_item_biases)

            self.tf_prediction_serial = bias_prediction_serial(tf_prediction_serial=self.tf_prediction_serial,
                                                               tf_projected_user_biases=tf_projected_user_biases,
                                                               tf_projected_item_biases=tf_projected_item_biases,
                                                               tf_x_user=tf_x_user,
                                                               tf_x_item=tf_x_item)

        tf_interactions_serial = tf_interactions.values
        self.tf_rankings = rank_predictions(tf_prediction=self.tf_prediction)

        # Loss function nodes
        self.tf_basic_loss = self.loss_graph_factory(tf_prediction_serial=self.tf_prediction_serial,
                                                     tf_interactions_serial=tf_interactions_serial,
                                                     tf_prediction=self.tf_prediction,
                                                     tf_interactions=tf_interactions,
                                                     tf_rankings=self.tf_rankings)

        self.tf_weight_reg_loss = sum(tf.nn.l2_loss(weights) for weights in tf_weights)
        self.tf_loss = self.tf_basic_loss + (self.tf_alpha * self.tf_weight_reg_loss)
        self.tf_optimizer = tf.train.AdamOptimizer(learning_rate=self.tf_learning_rate).minimize(self.tf_loss)

        # Get node names for each graph hook
        for graph_tensor_hook_attr_name in self.graph_tensor_hook_attr_names:
            hook = self.__getattribute__(graph_tensor_hook_attr_name)
            self.graph_tensor_hook_node_names[graph_tensor_hook_attr_name] = hook.name
        for graph_operation_hook_attr_name in self.graph_operation_hook_attr_names:
            hook = self.__getattribute__(graph_operation_hook_attr_name)
            self.graph_operation_hook_node_names[graph_operation_hook_attr_name] = hook.name

    def fit(self, interactions, user_features, item_features, epochs=100, learning_rate=0.1, alpha=0.0001,
            verbose=False, out_sample_interactions=None):
        """
        Constructs the TensorRec graph and fits the model.
        :param interactions: scipy.sparse matrix
        A matrix of interactions of shape [n_users, n_items].
        :param user_features: scipy.sparse matrix
        A matrix of user features of shape [n_users, n_user_features].
        :param item_features: scipy.sparse matrix
        A matrix of item features of shape [n_items, n_item_features].
        :param epochs: Integer
        The number of epochs to fit the model.
        :param learning_rate: Float
        The learning rate of the model.
        :param alpha:
        The weight regularization loss coefficient.
        :param verbose: boolean
        If true, the model will print a number of status statements during fitting.
        :param out_sample_interactions: scipy.sparse matrix
        A matrix of interactions of shape [n_users, n_items].
        If not None, and verbose == True, the model will be evaluated on these interactions on every epoch.
        """

        # Pass-through to fit_partial
        self.fit_partial(interactions, user_features, item_features, epochs, learning_rate, alpha, verbose,
                         out_sample_interactions)

    def fit_partial(self, interactions, user_features, item_features, epochs=1, learning_rate=0.1,
                    alpha=0.0001, verbose=False, out_sample_interactions=None):
        """
        Constructs the TensorRec graph and fits the model.
        :param interactions: scipy.sparse matrix
        A matrix of interactions of shape [n_users, n_items].
        :param user_features: scipy.sparse matrix
        A matrix of user features of shape [n_users, n_user_features].
        :param item_features: scipy.sparse matrix
        A matrix of item features of shape [n_items, n_item_features].
        :param epochs: Integer
        The number of epochs to fit the model.
        :param learning_rate: Float
        The learning rate of the model.
        :param alpha:
        The weight regularization loss coefficient.
        :param verbose: boolean
        If true, the model will print a number of status statements during fitting.
        :param out_sample_interactions: scipy.sparse matrix
        A matrix of interactions of shape [n_users, n_items].
        If not None, and verbose == True, the model will be evaluated on these interactions on every epoch.
        """

        session = get_session()

        # Check if the graph has been constructed by checking the dense prediction node
        # If it hasn't been constructed, initialize it
        if self.tf_prediction is None:
            # Numbers of features are learned at fit time from the shape of these two matrices and cannot be changed
            # without refitting
            self._build_tf_graph(n_user_features=user_features.shape[1], n_item_features=item_features.shape[1])
            session.run(tf.global_variables_initializer())

        if verbose:
            logging.info('Processing interaction and feature data')

        feed_dict = self._create_feed_dict(interactions, user_features, item_features,
                                           extra_feed_kwargs={self.tf_learning_rate: learning_rate,
                                                              self.tf_alpha: alpha})

        if verbose:
            logging.info('Beginning fitting')

        for epoch in range(epochs):

            session.run(self.tf_optimizer, feed_dict=feed_dict)

            if verbose:
                mean_loss = self.tf_basic_loss.eval(session=session, feed_dict=feed_dict)
                mean_pred = np.mean(self.tf_prediction_serial.eval(session=session, feed_dict=feed_dict))
                weight_reg_l2_loss = (alpha * self.tf_weight_reg_loss).eval(session=session, feed_dict=feed_dict)
                logging.info('EPOCH %s loss = %s, weight_reg_l2_loss = %s, mean_pred = %s' % (epoch, mean_loss,
                                                                                       weight_reg_l2_loss, mean_pred))
                if out_sample_interactions:
                    os_feed_dict = self._create_feed_dict(out_sample_interactions, user_features, item_features)
                    os_loss = self.tf_basic_loss.eval(session=session, feed_dict=os_feed_dict)
                    logging.info('Out-Sample loss = %s' % os_loss)

    def predict(self, user_features, item_features):
        """
        Predict recommendation scores for the given users and items.
        :param user_features: scipy.sparse matrix
        A matrix of user features of shape [n_users, n_user_features].
        :param item_features: scipy.sparse matrix
        A matrix of item features of shape [n_items, n_item_features].
        :return: TBD
        """
        feed_dict = self._create_feed_dict(interactions_matrix=None,
                                           user_features_matrix=user_features,
                                           item_features_matrix=item_features)

        predictions = self.tf_prediction.eval(session=get_session(), feed_dict=feed_dict)

        return predictions

    def predict_rank(self, user_features, item_features):
        """
        Predict recommendation ranks for the given users and items.
        :param user_features: scipy.sparse matrix
        A matrix of user features of shape [n_users, n_user_features].
        :param item_features: scipy.sparse matrix
        A matrix of item features of shape [n_items, n_item_features].
        :return: TBD
        """
        feed_dict = self._create_feed_dict(interactions_matrix=None,
                                           user_features_matrix=user_features,
                                           item_features_matrix=item_features)

        rankings = self.tf_rankings.eval(session=get_session(), feed_dict=feed_dict)

        return rankings

    def predict_user_representation(self, user_features):
        """
        Predict representation vectors for the given users.
        :param user_features: scipy.sparse matrix
        A matrix of user features of shape [n_users, n_user_features].
        :return: TBD
        """
        if self.biased:
            raise NotImplementedError('predict_user_representation() is not supported with biased models.'
                                      'Try TensorRec(biased=False)')

        feed_dict = self._create_user_feed_dict(user_features_matrix=user_features)
        user_repr = self.tf_user_representation.eval(session=get_session(), feed_dict=feed_dict)
        return user_repr

    def predict_item_representation(self, item_features):
        """
        Predict representation vectors for the given items.
        :param item_features: scipy.sparse matrix
        A matrix of item features of shape [n_items, n_item_features].
        :return: TBD
        """
        if self.biased:
            raise NotImplementedError('predict_item_representation() is not supported with biased models.'
                                      'Try TensorRec(biased=False)')

        feed_dict = self._create_item_feed_dict(item_features_matrix=item_features)
        item_repr = self.tf_item_representation.eval(session=get_session(), feed_dict=feed_dict)
        return item_repr

    def save_model(self, directory_path):
        """
        Saves the model to files in the given directory.
        :param directory_path: str
        The path to the directory in which to save the model.
        :return:
        """

        saver = tf.train.Saver()
        session_path = '%s/tensorrec_session.cpkt' % directory_path
        saver.save(sess=get_session(), save_path=session_path)

        # Break connections to the graph before saving the python object
        self._clear_graph_hook_attrs()
        tensorrec_path = '%s/tensorrec.pkl' % directory_path
        with open(tensorrec_path, 'wb') as file:
            pickle.dump(file=file, obj=self)

        # Reconnect to the graph after saving
        self._attach_graph_hook_attrs()

    @classmethod
    def load_model(cls, directory_path):
        """
        Loads the TensorRec model and TensorFlow session saved in the given directory.
        :param directory_path: str
        The path to the directory containing the saved model.
        :return:
        """

        saver = tf.train.Saver()
        session_path = '%s/tensorrec_session.cpkt' % directory_path
        saver.restore(sess=get_session(), save_path=session_path)

        tensorrec_path = '%s/tensorrec.pkl' % directory_path
        with open(tensorrec_path, 'rb') as file:
            model = pickle.load(file=file)
        model._attach_graph_hook_attrs()
        return model
