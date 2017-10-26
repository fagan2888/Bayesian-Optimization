from gpflow.gpr import GPR
import numpy as np
import tensorflow as tf
import random
import copy
from .solvers import solve
import sys


class BO(GPR):
    '''
    This is a simple (abstract) implementation of Bayesian Optimization.
    It extends gpflow's GPR.
    '''
    def __init__(self, options):
        self.bounds = options['objective'].bounds.copy()
        self.dim = self.bounds.shape[0]

        if 'mean_function' not in options:
            options['mean_function'] = None
        super(BO, self).__init__(X=np.zeros((0, self.dim)),
                                 Y=np.zeros((0, self.dim)),
                                 kern=options['kernel'],
                                 mean_function=options['mean_function']
                                 )

        # Unpack some commonly used parameters
        self.iterations = options['iterations']
        self.batch_size = options['batch_size']
        self.initial_size = options['initial_size']
        self.hessian = options['hessian']

        self.options = options.copy()

        # Fix the noise, if its value is provided
        if self.options['noise'] is not None:
            self.likelihood.variance = self.options['noise']
            self.likelihood.variance.fixed = True

    def bayesian_optimization(self, seed):
        '''
        This function implements the main loop of Bayesian Optimization
        '''
        self.seed = seed

        # Set random seed: Numpy, Tensorflow, Python 
        tf.set_random_seed(seed)
        seed(seed)
        random.seed(seed)

        # Copy the objective. This is essential when testing draws from GPs
        objective = copy.copy(self.options['objective'])
        X0 = self.random_sample(self.bounds, self.initial_size)
        y0 = objective.f(X0)

        # Set the data to the GP model
        # Careful, we might normalize the function evaluations
        # Provide only the first column of y0.
        # The others columns contain auxiliary data.
        self.X = X0
        self.Y = self.normalize(y0[:, 0:1])

        # X_all stores all the points where f was evaluated and
        # y_all the respective function values
        X_all = X0
        y_all = y0

        for i in range(self.iterations):
            self.optimize_restarts(restarts=self.options['model_restarts'])

            if self.options['samples'] > 0:
                # Draw samples of the hyperparameters from the posterior
                samples_raw = self.sample(self.options['samples'])
                self.samples = self.get_samples_df(samples_raw)

            # Evaluate the black-box function at the suggested points
            X_new = self.get_suggestion(self.batch_size)
            y_new = objective.f(X_new)

            # Append the algorithm's choice X_new to X_all
            # Add the function evaluations f(X_new) to y_all
            X_all = np.concatenate((X_all, X_new))
            y_all = np.concatenate((y_all, y_new))

            # Update the GP model
            # Careful, the model might normalize the function evaluations
            # Provide only the first column of y_all.
            # The others columns contain auxiliary data.
            self.X = X_all
            self.Y = self.normalize(y_all[:, 0:1])

        return X_all, y_all

    def get_suggestion(self, batch_size):
        X = None    # Will hold the final choice
        y = None    # Will hold the expected improvement of the final choice

        # Tile bounds to match batch size
        bounds_tiled = np.tile(self.bounds, (batch_size, 1))

        # Run local gradient-descent optimizer multiple times
        # to avoid getting stuck in a poor local optimum
        for j in range(self.options['opt_restarts']):
            # Initial point of the optimization
            X_init = self.random_sample(self.bounds, batch_size)

            try:
                X0, y0, status = solve(X_init=X_init,
                                       bounds=bounds_tiled,
                                       hessian=self.hessian,
                                       bo=self,
                                       solver=self.options['nl_solver'])

                # Update X if the current local minimum is
                # the best one found so far
                if X is None or y0 < y:
                    X, y = X0, y0
            except KeyboardInterrupt:
                raise
            except:
                print('Optimization #', j,
                      'of the acquisition function failed!',
                      file=sys.stderr)

        # Assert that at least one optimization run succesfully
        assert X is not None

        return X

    def acquisition(self, x):
        '''
        This function just reshapes x, which can be provided flat,
        to (batch_size, dim) and calls acquisition_tf
        '''
        k = x.size // self.dim
        X = x.reshape(k, self.dim)

        obj, gradient = self.acquisition_tf(X)

        return obj, gradient

    def optimize_restarts(self, restarts=1, **kwargs):
        '''
        Wrapper of self._objective to allow for multiple restarts
        '''
        if self._needs_recompile:
            self.compile()
        obj = self._objective

        par_min = self.get_free_state().copy()
        val_min = obj(par_min)[0]
        for i in range(restarts):
            try:
                self.randomize()
                self.optimize(**kwargs)
                x = self.get_free_state().copy()
                val = obj(x)[0]
            except KeyboardInterrupt:
                raise
            except:
                val = float("inf")

            if val < val_min:
                par_min = x
                val_min = val

        self.set_state(par_min)

    @staticmethod
    def random_sample(bounds, k):
        '''
        Generate a set of k n-dimensional points sampled uniformly at random
        Inputs:
            bounds: n x 2 dimenional array containing upper/lower bounds
                    for each dimension
            k: number of points
        Output: k x n array containing the sampled points
        '''
        # k: Number of points
        n = bounds.shape[0]  # Dimensionality of each point
        X = np.zeros((k, n))
        for i in range(n):
            X[:, i] = np.random.uniform(bounds[i, 0], bounds[i, 1], k)

        return X

    def normalize(self, Y):
        '''
        When normalization is enabled, this function normalizes the first
        collumn of Y to have zero mean and std one.

        Recall that the first collumn contains the output of the function under
        minimization. The rest of the collumns (if any) contain auxiliary data
        that are only used for inspection purposes.
        '''

        Y_ = Y.copy()
        if self.options['normalize_Y'] and np.std(Y[:, 0]) > 0:
            Y_[:, 0] = (Y[:, 0] - np.mean(Y[:, 0]))/np.std(Y[:, 0])

        return Y_
