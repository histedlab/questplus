from typing import Optional
import xarray as xr
import numpy as np
from copy import deepcopy

from questplus import psychometric_function


class QuestPlus:
    def __init__(self, *,
                 stim_domain: dict,
                 param_domain: dict,
                 outcome_domain: dict,
                 prior: Optional[dict] = None,
                 func: str = 'weibull',
                 stim_scale: str,
                 stim_selection_method: str = 'min_entropy',
                 stim_selection_options: Optional[dict] = None,
                 param_estimation_method: str = 'mean'):
        """
        A QUEST+ staircase procedure.

        Parameters
        ----------
        stim_domain
            Specification of the stimulus domain: dictionary keys correspond to
            the names of the stimulus dimensions, and  values describe the
            respective possible stimulus values (e.g., intensities, contrasts,
            or orientations).

        param_domain
            Specification of the parameter domain: dictionary keys correspond
            to the names of the parameter dimensions, and  values describe the
            respective possible parameter values (e.g., threshold, slope,
            lapse rate.

        outcome_domain
            Specification of the outcome domain: dictionary keys correspond
            to the names of the outcome dimensions, and  values describe the
            respective possible outcome values (e.g., "Yes", "No", "Correct",
            "Incorrect"). This argument typically describes the responses a
            participant can provide.

        prior
            A-priori probabilities of parameter values.

        func
            The psychometric function whose parameters to estimate. Currently
            supported are the Weibull function, `weibull`, and the spatio-
            temporal contrast sensitivity function, `csf`.

        stim_scale
            The scale on which the stimuli are provided. Currently supported
            are the decadic logarithm, `log10`; and decibels, `dB`.

        stim_selection_method
            How to select the next stimulus. `min_entropy` picks the stimulus
            that will minimize the expected entropy. `min_n_entropy` randomly
            selects a stimulus from the set of stimuli that will yield the `n`
            smallest entropies. `n` has to be specified via the
            `stim_selection_options` keyword argument.

        stim_selection_options
            Use this argument to specify options for the stimulus selection
            method specified via `stim_selection_method`. Currently, this is
            only used to specify the number of `n` stimuli that will yield the
            `n` smallest entropies `stim_selection_method=min_n_entropy`.

        param_estimation_method
            The method to use when deriving the final parameter estimate.
            Possible values are `mean` (mean of each parameter, weighted by the
            posterior probabilities) and `mode` (the parameters at the peak of
            the posterior distribution).

        """
        self.func = func
        self.stim_scale = stim_scale
        self.stim_domain = self._ensure_ndarray(stim_domain)
        self.param_domain = self._ensure_ndarray(param_domain)
        self.outcome_domain = self._ensure_ndarray(outcome_domain)

        self.prior = self._gen_prior(prior=prior)
        self.posterior = deepcopy(self.prior)
        self.likelihoods = self._gen_likelihoods()

        self.stim_selection = stim_selection_method
        self.stim_selection_options = stim_selection_options

        self.param_estimation_method = param_estimation_method

        self.resp_history = list()
        self.stim_history = {p: [] for p in self.stim_domain.keys()}
        self.entropy = np.nan

    @staticmethod
    def _ensure_ndarray(x: dict) -> dict:
        x = deepcopy(x)
        for k, v in x.items():
            x[k] = np.atleast_1d(v)

        return x

    def _gen_prior(self, *,
                   prior: dict) -> xr.DataArray:
        prior_orig = deepcopy(prior)

        if prior_orig is None:
            prior = np.ones([len(x) for x in self.param_domain.values()])
        else:
            prior_grid = np.meshgrid(*list(prior_orig.values()),
                                     sparse=True, indexing='ij')
            prior = np.prod(prior_grid)

        # Normalize.
        prior /= prior.sum()

        dims = *self.param_domain.keys(),
        coords = dict(**self.param_domain)
        prior_ = xr.DataArray(data=prior,
                              dims=dims,
                              coords=coords)

        return prior_

    def _gen_likelihoods(self) -> xr.DataArray:
        outcome_dim_name = list(self.outcome_domain.keys())[0]
        outcome_values = list(self.outcome_domain.values())[0]

        if self.func in ['weibull', 'csf', 'norm_cdf', 'norm_cdf_2']:
            if self.func == 'weibull':
                f = psychometric_function.weibull
            elif self.func == 'csf':
                f = psychometric_function.csf
            elif self.func == 'norm_cdf':
                f = psychometric_function.norm_cdf
            else:
                f = psychometric_function.norm_cdf_2

            prop_correct = f(**self.stim_domain,
                             **self.param_domain,
                             scale=self.stim_scale)

            prop_incorrect = 1 - prop_correct

            # Now this is a bit awkward. We concatenate the psychometric
            # functions for the different responses. To do that, we first have
            # to add an additional dimension.
            # TODO: There's got to be a neater way to do this?!
            corr_resp_dim = {outcome_dim_name: [outcome_values[0]]}
            inccorr_resp_dim = {outcome_dim_name: [outcome_values[1]]}

            prop_correct = prop_correct.expand_dims(corr_resp_dim)
            prop_incorrect = prop_incorrect.expand_dims(inccorr_resp_dim)

            pf_values = xr.concat([prop_correct, prop_incorrect],
                                  dim=outcome_dim_name,
                                  coords=self.outcome_domain)
        else:
            raise ValueError('Unknown psychometric function name specified.')

        return pf_values

    def update(self, *,
               stim: dict,
               outcome: dict) -> None:
        """
        Inform QUEST+ about a newly gathered measurement outcome for a given
        stimulus parameter set, and update the posterior accordingly.

        Parameters
        ----------
        stim
            The stimulus that was used to generate the given outcome.

        outcome
            The observed outcome.

        """
        likelihood = (self.likelihoods
                      .sel(**stim, **outcome))

        self.posterior = self.posterior * likelihood
        self.posterior /= self.posterior.sum()

        # Log the results, too.
        for stim_property, stim_val in stim.items():
            self.stim_history[stim_property].append(stim_val)
        self.resp_history.append(outcome)

    @property
    def next_stim(self) -> dict:
        """
        Retrieve the stimulus to present next.

        The stimulus will be selected based on the method in
        `self.stim_selection`.

        Returns
        -------
        The stimulus to present next.

        """
        stim_selection = self.stim_selection
        new_posterior = self.posterior * self.likelihoods

        # Probability.
        pk = new_posterior.sum(dim=self.param_domain.keys())
        new_posterior /= pk

        # Entropies.
        # Note that np.log(0) returns nan; xr.DataArray.sum() has special
        # handling for this case.
        H = -((new_posterior * np.log(new_posterior))
              .sum(dim=self.param_domain.keys()))

        # Expected entropies for all possible stimulus parameters.
        EH = (pk * H).sum(dim=list(self.outcome_domain.keys()))

        if stim_selection == 'min_entropy':
            # Get coordinates of stimulus properties that minimize entropy.
            index = np.unravel_index(EH.argmin(), EH.shape)
            coords = EH[index].coords
            stim = {stim_property: stim_val.item()
                    for stim_property, stim_val in coords.items()}
            self.entropy = EH.min().item()
        # FIXME: currently disabled, need to adopt above method for
        # finding correct coordinates!
        # elif stim_selection == 'min_n_entropy':
        #     index = np.argsort(EH)[:4]
        #     while True:
        #         stim_candidates = self.stim_domain['intensity'][index.values]
        #         stim = np.random.choice(stim_candidates)
        #
        #         if len(self.stim_history['intensity']) < 2:
        #             break
        #         elif (np.isclose(stim, self.stim_history['intensity'][-1]) and
        #               np.isclose(stim, self.stim_history['intensity'][-2])):
        #             print('\n  ==> shuffling again... <==\n')
        #             continue
        #         else:
        #             break
        #
        #     print(f'options: {self.stim_domain["intensity"][index.values]} -> {stim}')
        else:
            raise ValueError('Unknown stim_selection supplied.')

        return stim

    @property
    def param_estimate(self) -> dict:
        """
        Retrieve the final parameter estimates after the QUEST+  run.

        The parameters will be calculated according to
        `self.param_estimation_method`.

        Returns
        -------
        A dictionary of parameter estimates, where the dictionary keys
        correspond to the parameter names.

        """
        method = self.param_estimation_method
        param_estimates = dict()
        for param_name in self.param_domain.keys():
            params = list(self.param_domain.keys())
            params.remove(param_name)

            if method == 'mean':
                param_estimates[param_name] = ((self.posterior.sum(dim=params) *
                                                self.param_domain[param_name])
                                               .sum()
                                               .item())
            elif method == 'mode':
                index = np.unravel_index(self.posterior.argmax(),
                                         self.posterior.shape)
                coords = self.posterior[index].coords
                param_estimates[param_name] = coords[param_name].item()
            else:
                raise ValueError('Unknown method parameter.')

        return param_estimates
