import abc
import collections
import math
import random
import typing

from river import base, drift, metrics, utils

ModelWrapper = collections.namedtuple("ModelWrapper", "model metric")


# TODO: change class inheritance
class SSPT(base.Estimator):
    _START_RANDOM = "random"
    _START_WARM = "warm"

    def __init__(
        self,
        model,
        metric: metrics.base.Metric,
        params_range: typing.Dict[str, typing.Tuple],
        grace_period: int = 500,
        drift_detector: base.DriftDetector = drift.ADWIN(),
        start: str = "warm",
        convergence_sphere: float = 0.01,
        seed: int = None,
    ):
        self.model = model
        self.metric = metric
        self.params_range = params_range

        self.grace_period = grace_period
        self.drift_detector = drift_detector

        if start not in {self._START_RANDOM, self._START_WARM}:
            raise ValueError(
                f"'start' must be either '{self._START_RANDOM}' or '{self._START_WARM}'."
            )
        self.start = start
        self.convergence_sphere = convergence_sphere

        self.seed = seed

        self._best_model = None
        self._simplex = self._create_simplex(model)

        # Models expanded from the simplex
        self._expanded: typing.Optional[typing.Dict] = None

        self._n = 0
        self._converged = False
        self._rng = random.Random(self.seed)

    def _random_config(self):
        config = {}

        for p_name, (p_type, p_range) in self.params_range.items():
            if p_type == int:
                config[p_name] = self._rng.randint(p_range[0], p_range[1])
            elif p_type == float:
                config[p_name] = self._rng.uniform(p_range[0], p_range[1])

        return config

    def _create_simplex(self, model) -> typing.List:
        # The simplex is divided in:
        # * 0: the best model
        # * 1: the 'good' model
        # * 2: the worst model
        simplex = [None] * 3

        simplex[0] = ModelWrapper(
            self.model.clone(self._random_config()), self.metric.clone()
        )
        simplex[2] = ModelWrapper(
            self.model.clone(self._random_config()), self.metric.clone()
        )

        g_params = model._get_params()
        if self.start == self._START_RANDOM:
            # The intermediate 'good' model is defined randomly
            g_params = self._random_config()

        simplex[1] = ModelWrapper(self.model.clone(g_params), self.metric.clone())

        return simplex

    def _sort_simplex(self):
        """Ensure the simplex models are ordered by predictive performance."""
        if self.metric.bigger_is_better:
            self._simplex.sort(key=lambda mw: mw.metric.get(), reverse=True)
        else:
            self._simplex.sort(key=lambda mw: mw.metric.get())

    def _nelder_mead_expansion(self) -> typing.Dict:
        """Create expanded models given the simplex models."""

        def apply_operator(m1, m2, func):
            new_config = {}
            m1_params = m1.model._get_params()
            m2_params = m2.model._get_params()

            for p_name, (p_type, p_range) in self.params_range.items():
                new_val = func(m1_params[p_name], m2_params[p_name])

                # Range sanity checks
                if new_val < p_range[0]:
                    new_val = p_range[0]
                if new_val > p_range[1]:
                    new_val = p_range[1]

                new_config[p_name] = round(new_val, 0) if p_type == int else new_val

            # Modify the current best contender with the new hyperparameter values
            return ModelWrapper(
                self._simplex[0].mutate(new_config), self.metric.clone()
            )

        expanded = {}
        # Midpoint between 'best' and 'good'
        expanded["midpoint"] = apply_operator(
            self._simplex[0], self._simplex[1], lambda h1, h2: (h1 + h2) / 2
        )
        # Reflection of 'midpoint' towards 'worst'
        expanded["reflection"] = apply_operator(
            expanded["midpoint"], self._simplex[2], lambda h1, h2: 2 * h1 - h2
        )
        # Expand the 'reflection' point
        expanded["expansion"] = apply_operator(
            expanded["reflection"], expanded["midpoint"], lambda h1, h2: 2 * h1 - h2
        )
        # Shrink 'best' and 'worst'
        expanded["shrink"] = apply_operator(
            self._simplex[0], self._simplex[2], lambda h1, h2: (h1 + h2) / 2
        )
        # Contraction of 'midpoint' and 'worst'
        expanded["contraction"] = apply_operator(
            expanded["midpoint"], self._simplex[2], lambda h1, h2: (h1 + h2) / 2
        )

        return expanded

    def _nelder_mead_operators(self):
        b = self._simplex[0].metric
        g = self._simplex[1].metric
        w = self._simplex[2].metric
        r = self._expanded["reflection"].metric

        if r.is_better_than(g):
            if b.is_better_than(r):
                self._simplex[2] = self._expanded["reflection"]
            else:
                e = self._expanded["expansion"].metric
                if e.is_better_than(b):
                    self._simplex[2] = self._expanded["expansion"]
                else:
                    self._simplex[2] = self._expanded["reflection"]
        else:
            if r.is_better_than(w):
                self._simplex[2] = self._expanded["reflection"]
            else:
                c = self._expanded["contraction"].metric
                if c.is_better_than(w):
                    self._simplex[2] = self._expanded["contraction"]
                else:
                    s = self._expanded["shrink"].metric
                    if s.is_better_than(w):
                        self._simplex[2] = self._expanded["shrink"]
                    m = self._expanded["midpoint"].metric
                    if m.is_better_than(g):
                        self._simplex[1] = self._expanded["midpoint"]

    @property
    def _models_converged(self) -> bool:
        # 1. Simplex in sphere

        params_b = self._simplex[0].model._get_params()
        params_g = self._simplex[1].model._get_params()
        params_w = self._simplex[2].model._get_params()

        # Normalize params to ensure the contribute equally to the stopping criterion
        for p_name, (_, p_range) in self.params_range.items():
            scale = p_range[1] - p_range[0]
            params_b[p_name] = (params_b[p_name] - p_range[0]) / scale
            params_g[p_name] = (params_g[p_name] - p_range[0]) / scale
            params_w[p_name] = (params_w[p_name] - p_range[0]) / scale

        max_dist = max(
            [
                utils.math.minkowski_distance(params_b, params_g, p=2),
                utils.math.minkowski_distance(params_b, params_w, p=2),
                utils.math.minkowski_distance(params_g, params_w, p=2),
            ]
        )

        ndim = len(params_b)
        r_sphere = max_dist * math.sqrt((ndim / (2 * (ndim + 1))))

        if r_sphere < self.convergence_sphere:
            return True

        # TODO? 2. Simplex did not change

        return False

    @abc.abstractmethod
    def _drift_input(self, y_true, y_pred) -> typing.Union[int, float]:
        pass

    def _learn_converged(self, x, y):
        y_pred = self._best_model.predict_one(x)

        input = self._drift_input(y, y_pred)
        self.drift_detector.update(input)

        # We need to start the optimization process from scratch
        if self.drift_detector.drift_detected:
            self._converged = False
            self._simplex = self._create_simplex(self._best_model)

            # There is no proven best model right now
            self._best_model = None
            return

        self._best_model.learn_one(x, y)

    def _learn_not_converged(self, x, y):
        for wrap in self._simplex:
            y_pred = wrap.model.predict_one(x)
            wrap.metric.update(y, y_pred)
            wrap.model.learn_one(x, y)

        if not self._expanded:
            self._expanded = self._nelder_mead_expansion()

        for wrap in self._expanded.values():
            y_pred = wrap.model.predict_one(x)
            wrap.metric.update(y, y_pred)
            wrap.model.learn_one(x, y)

        if self._n == self.grace_period:
            self._n = 0

            self._sort_simplex()
            # Update the simplex models using Nelder-Mead heuristics
            self._nelder_mead_operators()

            # Discard expanded models
            self._expanded = None

        if self._models_converged:
            self._converged = True
            self._best_model = self._simplex[0].model

    def learn_one(self, x, y):
        self._n += 1

        if self.converged:
            self._learn_converged(x, y)
        else:
            self._learn_not_converged(x, y)

    @property
    def best_model(self):
        if not self._converged:
            # Lazy selection of the best model
            self._sort_simplex()
            return self._simplex[0].model

        return self._best_model

    @property
    def converged(self):
        return self._converged