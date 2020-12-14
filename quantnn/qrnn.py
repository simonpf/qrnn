"""
quantnn.qrnn
============

This module provides the QRNN class, which implements the high-level
functionality of quantile regression neural networks, while the neural
network implementation is left to the model backends implemented in the
``quantnn.models`` submodule.
"""
import copy
import pickle
import importlib

import numpy as np
import quantnn.functional as qf
from quantnn.common import QuantnnException, UnsupportedBackendException

################################################################################
# Set the backend
################################################################################

#
# Try and load a supported backend.
#

try:
    import quantnn.models.keras as keras
    backend = keras
except Exception:
    pass

try:
    import quantnn.retrieval.qrnn.models.pytorch as pytorch
    backend = pytorch
except Exception:
    pass


def set_backend(name):
    """
    Set the neural network package to use as backend.

    The currently available backend are "keras" and "pytorch".

    Args:
        name(str): The name of the backend.
    """
    global backend
    if name == "keras":
        try:
            import quantnn.models.keras as keras
            backend = keras
        except Exception as e:
            raise Exception("The following error occurred while trying "
                            " to import keras: ", e)
    elif name == "pytorch":
        try:
            import quantnn.models.pytorch as pytorch
            backend = pytorch
        except Exception as e:
            raise Exception("The following error occurred while trying "
                            " to import pytorch: ", e)
    else:
        raise Exception("\"{}\" is not a supported backend.".format(name))


def get_backend(name):
    """
    Get module object corresponding to the short backend name.

    The currently available backend are "keras" and "pytorch".

    Args:
        name(str): The name of the backend.
    """
    if name == "keras":
        try:
            import quantnn.models.keras as keras
            backend = keras
        except Exception as e:
            raise Exception("The following error occurred while trying "
                            " to import keras: ", e)
    elif name == "pytorch":
        try:
            import quantnn.models.pytorch as pytorch
            backend = pytorch
        except Exception as e:
            raise Exception("The following error occurred while trying "
                            " to import pytorch: ", e)
    else:
        raise Exception("\"{}\" is not a supported backend.".format(name))
    return backend


def create_model(input_dim,
                 output_dim,
                 arch):
    """
    Creates a fully-connected neural network from a tuple
    describing its architecture.

    Args:
        input_dim(int): Number of input features.
        output_dim(int): Number of output features.
        arch: Tuple (d, w, a) containing the depth, i.e. number of
            hidden layers width of the hidden layers, i. e.
            the number of neurons in them, and the name of the
            activation function as string.
    Return:
        Depending on the available backends, a fully-connected
        keras or pytorch model, with the requested number of hidden
        layers and neurons in them.
    """
    return backend.FullyConnected(input_dim, output_dim, arch)


###############################################################################
# QRNN class
###############################################################################
class QRNN:
    r"""
    Quantile Regression Neural Network (QRNN)

    This class provides a high-level implementation of  quantile regression
    neural networks. It can be used to estimate quantiles of the posterior
    distribution of remote sensing retrievals.

    The :class:`QRNN`` class uses an arbitrary neural network model, that is
    trained to minimize the quantile loss function

    .. math::
            \mathcal{L}_\tau(y_\tau, y_{true}) =
            \begin{cases} (1 - \tau)|y_\tau - y_{true}| & \text{ if } y_\tau < y_\text{true} \\
            \tau |y_\tau - y_\text{true}| & \text{ otherwise, }\end{cases}

    where :math:`x_\text{true}` is the true value of the retrieval quantity
    and and :math:`x_\tau` is the predicted quantile. The neural network
    has one output neuron for each quantile to estimate.

    The QRNN class provides a generic QRNN implementation in the sense that it
    does not assume a fixed neural network architecture or implementation.
    Instead, this functionality is off-loaded to a model object, which can be
    an arbitrary regression network such as a fully-connected or a
    convolutional network. A range of different models are provided in the
    quantnn.models module. The :class:`QRNN`` class just
    implements high-level operation on the QRNN output while training and
    prediction are delegated to the model object. For details on the respective
    implementation refer to the documentation of the corresponding model class.

    .. note::

      For the QRNN implementation :math:`x` is used to denote the input
      vector and :math:`y` to denote the output. While this is opposed
      to inverse problem notation typically used for retrievals, it is
      in line with machine learning notation and felt more natural for
      the implementation. If this annoys you, I am sorry.

    Attributes:
        backend(``str``):
            The name of the backend used for the neural network model.
        quantiles (numpy.array):
            The 1D-array containing the quantiles :math:`\tau \in [0, 1]`
            that the network learns to predict.
        model:
            The neural network regression model used to predict the quantiles.
    """
    def __init__(self,
                 input_dimensions,
                 quantiles=None,
                 model=(3, 128, "relu"),
                 ensemble_size=1,
                 **kwargs):
        """
        Create a QRNN model.

        Arguments:
            input_dimensions(int):
                The dimension of the measurement space, i.e. the
                number of elements in a single measurement vector y
            quantiles(np.array):
                1D-array containing the quantiles  to estimate of
                the posterior distribution. Given as fractions within the range
                [0, 1].
            model:
                A (possibly trained) model instance or a tuple
                ``(d, w, act)`` describing the architecture of a
                fully-connected neural network with :code:`d` hidden layers with
                :code:`w` neurons and :code:`act` activation functions.
        """
        self.input_dimensions = input_dimensions
        self.quantiles = np.array(quantiles)

        # Provided model is just an architecture tuple
        if type(model) == tuple:
            model = backend.FullyConnected(self.input_dimensions,
                                           self.quantiles,
                                           model)
            if quantiles is None:
                raise QuantnnException("If model is given as architecture tuple"
                                       ", the 'quantiles' kwarg must be "
                                       "provided.")
        # Provided model is predefined model.
        else:
            # Determine module and check if supported.
            module = model.__module__.split(".")[0]
            if module not in ["keras", "torch"]:
                raise UnsupportedBackendException(
                    "The provided model comes from a unsupported "
                    "backend module. ")
            set_backend(model.__module__.split(".")[0])

            # Quantiles kwarg is provided.
            if quantiles:
                if hasattr(model, "quantiles"):
                    if not all(quantiles == model.quantiles):
                        raise QuantnnException(
                            "Provided quantiles do not match those of "
                            "the provided model."
                        )
                model.quantiles = quantiles
            # Quantiles kwarg is not provided.
            else:
                if not hasattr(model, "quantiles"):
                    raise QuantnnException(
                        "If 'quantiles' kwarg is not provided, the provided"
                        "neural network model must have a 'quantiles'"
                        " attribute."
                    )
                self.quantiles = model.quantiles

        self.model = model
        self.backend = backend.__name__

    def train(self,
              training_data,
              validation_data=None,
              batch_size=256,
              sigma_noise=None,
              adversarial_training=False,
              delta_at=0.01,
              initial_learning_rate=1e-2,
              momentum=0.0,
              convergence_epochs=5,
              learning_rate_decay=2.0,
              learning_rate_minimum=1e-6,
              maximum_epochs=200,
              training_split=0.9,
              optimizer=None,
              learning_rate_scheduler=None,
              gpu = False):
        """
        Train model on given training data.

        The training is performed on the provided training data and an
        optionally-provided validation set. Training can use the following
        augmentation methods:
            - Gaussian noise added to input
            - Adversarial training
        The learning rate is decreased gradually when the validation or training
        loss did not decrease for a given number of epochs.

        Args:
            training_data: Tuple of numpy arrays of a dataset object to use to
                train the model.
            validation_data: Optional validation data in the same format as the
                training data.
            batch_size: If training data is provided as arrays, this batch size
                will be used to for the training.
            sigma_noise: If training data is provided as arrays, training data
                will be augmented by adding noise with the given standard
                deviations to each input vector before it is presented to the
                model.
            adversarial_training(``bool``): Whether or not to perform
                adversarial training using the fast gradient sign method.
            delta_at: The scaling factor to apply for adversarial training.
            initial_learning_rate(``float``): The learning rate with which the
                 training is started.
            momentum(``float``): The momentum to use for training.
            convergence_epochs(``int``): The number of epochs with
                 non-decreasing loss before the learning rate is decreased
            learning_rate_decay(``float``): The factor by which the learning rate
                 is decreased.
            learning_rate_minimum(``float``): The learning rate at which the
                 training is aborted.
            maximum_epochs(``int``): For how many epochs to keep training.
            training_split(``float``): If no validation data is provided, this
                 is the fraction of training data that is used for validation.
            gpu(``bool``): Whether or not to try to run the training on the GPU.
        """
        return self.model.train(training_data,
                                validation_data=validation_data,
                                batch_size=batch_size,
                                sigma_noise=sigma_noise,
                                adversarial_training=adversarial_training,
                                delta_at=delta_at,
                                initial_learning_rate=initial_learning_rate,
                                momentum=momentum,
                                convergence_epochs=convergence_epochs,
                                learning_rate_decay=learning_rate_decay,
                                learning_rate_minimum=learning_rate_minimum,
                                maximum_epochs=maximum_epochs,
                                training_split=training_split,
                                gpu=gpu,
                                optimizer=optimizer,
                                learning_rate_scheduler=learning_rate_scheduler)

    def predict(self, x):
        r"""
        Predict quantiles of the conditional distribution P(y|x).

        Forward propagates the inputs in `x` through the network to
        obtain the predicted quantiles `y`.

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` m-dimensional inputs
                         for which to predict the conditional quantiles.

        Returns:

             Array of shape `(n, k)` with the columns corresponding to the k
             quantiles of the network.

        """
        return self.model.predict(x)

    def cdf(self, x):
        r"""
        Approximate the posterior CDF for given inputs `x`.

        Propagates the inputs in `x` forward through the network and
        approximates the posterior CDF by a piecewise linear function.

        The piecewise linear function is given by its values at approximate
        quantiles :math:`x_\tau`` for :math:`\tau = \{0.0, \tau_1, \ldots,
        \tau_k, 1.0\}` where :math:`\tau_k` are the quantiles to be estimated
        by the network. The values for :math:`x_{0.0}` and :math:`x_{1.0}` are
        computed using

        .. math::

            x_{0.0} = 2.0 x_{\tau_1} - x_{\tau_2}

            x_{1.0} = 2.0 x_{\tau_k} - x_{\tau_{k-1}}

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` inputs for which
                         to predict the conditional quantiles.

        Returns:

            Tuple (xs, fs) containing the :math:`x`-values in `xs` and corresponding
            values of the posterior CDF :math:`F(x)` in `fs`.

        """
        y_pred = self.predict(x)
        return qf.cdf(y_pred, self.quantiles, quantile_axis=1)

    def calibration(self, *args, **kwargs):
        """
        Compute calibration curve for the given dataset.
        """
        return self.model.calibration(*args, *kwargs)

    def pdf(self, x):
        r"""
        Approximate the posterior probability density function (PDF) for given
        inputs ``x``.

        The PDF is approximated by computing the derivative of the piece-wise
        linear approximation of the CDF as computed by the
        :py:meth:`quantnn.QRNN.cdf` function.

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` inputs for which
               to predict PDFs.

        Returns:

            Tuple (x_pdf, y_pdf) containing the array with shape `(n, k)`  containing
            the x and y coordinates describing the PDF for the inputs in ``x``.

        """
        y_pred = self.predict(x)
        return qf.pdf(y_pred, self.quantiles, quantile_axis=1)

    def sample_posterior(self, x, n_samples=1):
        r"""
        Generates :code:`n` samples from the estimated posterior
        distribution for the input vector :code:`x`. The sampling
        is performed by the inverse CDF method using the estimated
        CDF obtained from the :code:`cdf` member function.

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` inputs for which
                         to predict the conditional quantiles.

            n(int): The number of samples to generate.

        Returns:

            Tuple (xs, fs) containing the :math:`x`-values in `xs` and corresponding
            values of the posterior CDF :math: `F(x)` in `fs`.
        """
        y_pred = self.predict(x)
        return qf.sample_posterior(y_pred,
                                   self.quantiles,
                                   n_samples=n_samples,
                                   quantile_axis=1)

    def sample_posterior_gaussian_fit(self, x, n_samples=1):
        r"""
        Generates :code:`n` samples from the estimated posterior
        distribution for the input vector :code:`x`. The sampling
        is performed by the inverse CDF method using the estimated
        CDF obtained from the :code:`cdf` member function.

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` inputs for which
                         to predict the conditional quantiles.

            n(int): The number of samples to generate.

        Returns:

            Tuple (xs, fs) containing the :math:`x`-values in `xs` and corresponding
            values of the posterior CDF :math: `F(x)` in `fs`.
        """
        y_pred = self.predict(x)
        return qf.sample_posterior_gaussian(y_pred,
                                            self.quantiles,
                                            n_samples=n_samples,
                                            quantile_axis=1)

    def posterior_mean(self, x):
        r"""
        Computes the posterior mean by computing the first moment of the
        estimated posterior CDF.

        Arguments:

            x(np.array): Array of shape `(n, m)` containing `n` inputs for which
                         to predict the posterior mean.
        Returns:

            Array containing the posterior means for the provided inputs.
        """
        y_pred = self.predict(x)
        return qf.posterior_mean(y_pred,
                                 self.quantiles,
                                 quantile_axis=1)

    def crps(y_pred, y_true, quantiles):
        r"""
        Compute the Continuous Ranked Probability Score (CRPS) for given quantile
        predictions.

        This function uses a piece-wise linear fit to the approximate posterior
        CDF obtained from the predicted quantiles in :code:`y_pred` to
        approximate the continuous ranked probability score (CRPS):

        .. math::
            CRPS(\mathbf{y}, x) = \int_{-\infty}^\infty (F_{x | \mathbf{y}}(x')
            - \mathrm{1}_{x < x'})^2 \: dx'

        Arguments:

            y_pred(numpy.array): Array of shape `(n, k)` containing the `k`
                                 estimated quantiles for each of the `n`
                                 predictions.

            y_test(numpy.array): Array containing the `n` true values, i.e.
                                 samples of the true conditional distribution
                                 estimated by the QRNN.

            quantiles: 1D array containing the `k` quantile fractions :math:`\tau`
                       that correspond to the columns in `y_pred`.

        Returns:

            `n`-element array containing the CRPS values for each of the
            predictions in `y_pred`.
        """
        y_pred = self.predict(x)
        return qf.crps(y_pred,
                       self.quantiles,
                       y_true,
                       quantile_axis=1)

    def probability_larger_than(self, x, y):
        """
        Classify output based on posterior PDF and given numeric threshold.

        Args:
            x: The input data as :code:`np.ndarray` or backend-specific
               dataset object.
            threshold: The numeric threshold to apply for classification.
        """
        y_pred = self.predict(x)
        return qf.probability_larger_than(y_pred,
                                          self.quantiles,
                                          y,
                                          quantile_axis=1)


    def probability_less_than(self, x, y):
        """
        Classify output based on posterior PDF and given numeric threshold.

        Args:
            x: The input data as :code:`np.ndarray` or backend-specific
               dataset object.
            threshold: The numeric threshold to apply for classification.
        """
        y_pred = self.predict(x)
        return qf.probability_less_than(y_pred,
                                        self.quantiles,
                                        y,
                                        quantile_axis=1)
    @staticmethod
    def load(path):
        r"""
        Load a model from a file.

        This loads a model that has been stored using the
        :py:meth:`quantnn.QRNN.save`  method.

        Arguments:

            path(str): The path from which to read the model.

        Return:

            The loaded QRNN object.
        """
        with open(path, 'rb') as f:
            qrnn = pickle.load(f)
            backend = importlib.import_module(qrnn.backend)
            model = backend.load_model(f, qrnn.quantiles)
            qrnn.model = model
        return qrnn

    def save(self, path):
        r"""
        Store the QRNN model in a file.

        This stores the model to a file using pickle for all attributes that
        support pickling. The Keras model is handled separately, since it can
        not be pickled.

        Arguments:

            path(str): The path including filename indicating where to
                       store the model.

        """
        f = open(path, "wb")
        pickle.dump(self, f)
        backend = importlib.import_module(self.backend)
        backend.save_model(f, self.model)
        f.close()


    def __getstate__(self):
        dct = copy.copy(self.__dict__)
        dct.pop("model")
        return dct

    def __setstate__(self, state):
        self.__dict__ = state
        self.models = None
