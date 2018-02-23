import tensorflow as tf


def project_biases(tf_features, n_features):
	"""
	Projects the biases from the feature space to calculate bias per actor
	:param tf_features:
	:param n_features:
	:return:
	"""
	tf_feature_biases = tf.Variable(tf.zeros([n_features, 1]))

	# The reduce sum is to perform a rank reduction
	tf_projected_biases = tf.reduce_sum(
		tf.sparse_tensor_dense_matmul(tf_features, tf_feature_biases),
		axis=1
	)

	return tf_feature_biases, tf_projected_biases


def split_sparse_tensor_indices(tf_sparse_tensor, n_dimensions):
	"""
	Separates the each dimension of a sparse tensor's indices into independent tensors
	:param tf_sparse_tensor:
	:param n_dimensions:
	:return:
	"""
	tf_transposed_indices = tf.transpose(tf_sparse_tensor.indices)
	return (tf_transposed_indices[i] for i in range(n_dimensions))


def prediction_serial(tf_user_representation, tf_item_representation, tf_x_user, tf_x_item):
	"""
	For the serial prediction case, reprs and biases are gathered based on user and item ids
	:param tf_user_representation:
	:param tf_item_representation:
	:param tf_x_user:
	:param tf_x_item:
	:return:
	"""
	gathered_user_reprs = tf.gather(tf_user_representation, tf_x_user)
	gathered_item_reprs = tf.gather(tf_item_representation, tf_x_item)
	return tf.reduce_sum(tf.multiply(gathered_user_reprs, gathered_item_reprs), axis=1)


def bias_prediction_dense(tf_prediction, tf_projected_user_biases, tf_projected_item_biases):
	"""
	Broadcasts user and item biases across their respective axes of the dense predictions
	:param tf_prediction:
	:param tf_projected_user_biases:
	:param tf_projected_item_biases:
	:return:
	"""
	return tf_prediction + tf.expand_dims(tf_projected_user_biases, 1) + tf.expand_dims(tf_projected_item_biases, 0)


def bias_prediction_serial(tf_prediction_serial, tf_projected_user_biases, tf_projected_item_biases, tf_x_user,
						   tf_x_item):
	"""
	Calculates the bias per user/item pair and adds it to the serial predictions
	:param tf_prediction_serial:
	:param tf_projected_user_biases:
	:param tf_projected_item_biases:
	:param tf_x_user:
	:param tf_x_item:
	:return:
	"""
	gathered_user_biases = tf.gather(tf_projected_user_biases, tf_x_user)
	gathered_item_biases = tf.gather(tf_projected_item_biases, tf_x_item)
	return tf_prediction_serial + gathered_user_biases + gathered_item_biases


def rank_predictions(tf_prediction):
	"""
	Double-sortation serves as a ranking process
	The +1 is so the top-ranked has a non-zero rank
	:param tf_prediction:
	:return:
	"""
	tf_prediction_item_size = tf.shape(tf_prediction)[1]
	tf_indices_of_ranks = tf.nn.top_k(tf_prediction, k=tf_prediction_item_size)[1]
	return tf.nn.top_k(-tf_indices_of_ranks, k=tf_prediction_item_size)[1] + 1
