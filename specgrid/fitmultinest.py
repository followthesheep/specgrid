from scipy.stats import norm, poisson
from collections import OrderedDict
from pylab import plt
import pymultinest
import json
import os
import numpy as np
from contextlib import contextmanager
import threading
import sys
import time

stdout_lock = threading.Lock()

class UniformPrior(object):
    """
    A Uniform distribution prior
    
    Parameters
    ----------
    
    lbound: ~float
        lower bound
    
    ubound: ~float
        upper bound
    """
    
    def __init__(self, lbound, ubound):
        self.lbound = lbound
        self.ubound = ubound
    
    def __call__(self, cube):
        return cube * (self.ubound - self.lbound) + self.lbound

class GaussianPrior(object):
    """
    A gaussian prior
    
    Parameters
    ----------
    
    m: ~float
        mean of the distribution
        
    sigma: ~float
        sigma of the distribution
    
    """
    
    def __init__(self, m, sigma):
        self.m = m
        self.sigma = sigma
        
    def __call__(self, cube):
        return norm.ppf(cube,scale=self.sigma,loc=self.m)
        

class PoissonPrior(object):
    """
    A Poisson prior
    
    Parameters
    ----------
    
    m: ~float
        mean of the distribution
        
    
    """
    def __init__(self, m):
        self.m = m
        
    def __call__(self,cube):
        return poisson.ppf(cube,loc=self.m)

class FixedPrior(object):
    """
    A fixed value
    
    Parameters
    ----------
    
    val: ~float
        fixed value
    """
    
    def __init__(self, val):
        self.val = val
    
    def __call__(self, cube):
        return self.val

@contextmanager
def set_stdout_parent(parent):
    """a context manager for setting a particular parent for sys.stdout

    the parent determines the destination cell of output
    """
    save_parent = sys.stdout.parent_header
    with stdout_lock:
        sys.stdout.parent_header = parent
        try:
            yield
        finally:
            # the flush is important, because that's when the parent_header actually has its effect
            sys.stdout.flush()
            sys.stdout.parent_header = save_parent

class ProgressPrinter(pymultinest.ProgressWatcher):
    """
        Continuously writes out the number of live and rejected points.
    """
    def run(self):
        import time
        thread_parent = sys.stdout.parent_header
        while self.running:
            time.sleep(self.interval_ms / 1000.)
            if not self.running:
                break
            try:
                with set_stdout_parent(thread_parent):
                    print(('rejected points: ', len(open(self.rejected, 'r').readlines())))
                    print(('alive points: ', len(open(self.live, 'r').readlines())))
            except Exception as e:
                print(e)


class Likelihood(object):
    """
    A spectrum likelihood object. Initialize with the spectral grid model.
    Calls will returns the log-likelihood for observing a spectrum given the model
    parameters and the uncertainty.
    
    Parameters
    ----------
    
    model_star: ~model from specpgrid
        model object containing the spectral grid to evaluate
        
    
    """    
    def __init__(self, spectrum, model_star, parameter_names):
        self.model = model_star
        self.parameter_names = parameter_names
        self.data = spectrum
        
        
    def __call__(self, model_param, ndim, nparam):
        # returns the likelihood of observing the data given the model parameters
        param_dict = OrderedDict([(key, value) for key, value in zip(self.parameter_names, model_param)])
        
        m = self.model.evaluate(**param_dict)
        
        # log-likelhood for chi-square
        return (-0.5 * ((self.data.flux.value - m.flux.value) / 
        self.data.uncertainty.value)**2).sum()

class FitMultinest(object):
    """
    Use multinest to fit a spectrum using a grid of models generated by specgrid.
    
    Parameters
    ----------
    
    spectrum: ~Spectrum1D from specpgrid
        Spectrum1D object with the observed spectrum
    priors: ~dict
        A dictionary with the parameters to fit as well as their priors as
        implemented in the prior classes available (UniformPrior, GaussianPrior,
        PoissonPrior,FixedPrior)
    model_star: ~model from specpgrid
        model object containing the spectral grid to evaluate
        
    likelihood: ~Likelihood object, optional
        By default uses the Likelihood object which uses the chi-square for the 
        likelihood of observing the data given the model parameters
    basename: ~string, optional
        The prefix for the files that will be output. 
        Default: basename='chains/spectrumFit_'
    """
    

    def __init__(self,spectrum, priors, model_star, likelihood=None, 
    basename='chains/spectrumFit_'):
        self.spectrum = spectrum    # Spectrum1D object with the data
        self.model = model_star     # grid of spectra from specgrid
        self.parameter_names = sorted(priors.keys(), key=lambda key: model_star.parameters.index(key))
        priors = OrderedDict([(key, priors[key]) for key in self.parameter_names])
        self.priors = PriorCollections(priors, 
                                parameter_order=model_star.parameters)  
                                # PriorCollection from the input priors
        self.n_params = len(priors)             # number of parameters to fit
        self.basename = basename                # prefix of the file names to save
        if likelihood is None:
            # use the default likelihood for a Spectrum1D object
            self.likelihood = Likelihood(self.spectrum, model_star, self.parameter_names)
            
        # variables that will be filled in after the fit has been run
        self.fit_mean = None    # mean of fitted parameters
        self.sigma = None      # 1 average sigma (68% credible intervales) for fitted parameters
        self.evidence = None   # the global evidence value for the best fit
        self.sigma1 = None     # the upper and lower range for 1 sigma around the mean
        self.sigma3 = None     # the upper and lower range for 3 sigma around the mean
        self.analyzer = None   # store the results of the pymultinest analyzer
        
    
    def run(self, no_plots=False, resume=False, verbose=False, **kwargs):
        # runs pymultinest
#        progress = ProgressPrinter(
#            n_params=self.n_params,
#            outputfiles_basename=self.basename)

#        progress.start()
        pymultinest.run(self.likelihood, self.priors.prior_transform,
                        self.n_params, outputfiles_basename=self.basename,
                        resume=resume, verbose = verbose, **kwargs)
        json.dump(self.parameter_names, open(self.basename+'params.json', 'w')) # save parameter names
        
        # analyze the output data
        a = pymultinest.Analyzer(outputfiles_basename=self.basename, n_params = self.n_params)
        self.analyzer = a

        # check to see if the required file exists before proceeding
        # (multinest has a limitation where the file name can only be
        # of a certain length, so the output file name might not be as
        # expected).
        if os.path.exists(self.basename+'stats.dat'):
            s = a.get_stats()
            self.stats = s   # the statistics on the chain
            modes = s['modes'][0]   
            self.mean = modes['mean']
            self.sigma = modes['sigma']
            self.evidence = s['global evidence']

            sigma1 = list()
            sigma3 = list()
            marginals = s['marginals']
            for i in np.arange(self.n_params):
                sigma1.append(marginals[i]['1sigma'])
                sigma3.append(marginals[i]['3sigma'])
            self.sigma1 = sigma1
            self.sigma3 = sigma3

            if not(no_plots):
                # try importing seaborn if it exists:
                try:
                    seaborn = __import__('seaborn')
                    seaborn.set_style('white')
                    seaborn.set_context("paper", font_scale=1.5, rc={"lines.linewidth": 1.0})
                except ImportError:
                    pass

                plt.figure() 
                plt.plot(self.spectrum.wavelength.value, self.spectrum.flux.value, color='red', label='data')

                param_dict = OrderedDict([(key, value) for key, value in zip(self.parameter_names, self.mean)])
                s2 = self.model.evaluate(**param_dict)
                # plot the mean of the posteriors for the parameters
                plt.plot(self.spectrum.wavelength.value, s2.flux.value, '-', color='blue', alpha=0.3, label='data')

                # for posterior_param in a.get_equal_weighted_posterior()[::100,:-1]:
                #     param_dict = OrderedDict([(key, value) for key, value in zip(self.parameter_names, posterior_param)])
                #     s2 = self.model.evaluate(**param_dict)
                # 	plt.plot(self.spectrum.wavelength.value, s2.flux.value, '-', color='blue', alpha=0.3, label='data')

                plt.savefig(self.basename + 'posterior.pdf')
                plt.close()            
                self.mkplots()
        
        
    def mkplots(self):
        # run to make plots of the resulting posteriors. Modified from marginal_plots.py
        # from pymultinest. Produces basename+marg.pdf and basename+marge.png files
        prefix = self.basename
        
        parameters = json.load(file(prefix + 'params.json'))
        n_params = len(parameters)
        
        a = pymultinest.Analyzer(n_params = n_params, outputfiles_basename = prefix)
        s = a.get_stats()
        
        p = pymultinest.PlotMarginal(a)
        
        try:
            values = a.get_equal_weighted_posterior()
        except IOError as e:
            print 'Unable to open: %s' % e
            return
            
        assert n_params == len(s['marginals'])
        modes = s['modes']

        dim2 = os.environ.get('D', '1' if n_params > 20 else '2') == '2'
        nbins = 100 if n_params < 3 else 20
        if dim2:
                plt.figure(figsize=(5.1*n_params, 5*n_params))
                for i in range(n_params):
                        plt.subplot(n_params, n_params, i + 1)
                        plt.xlabel(parameters[i])

                        m = s['marginals'][i]
                        plt.xlim(m['5sigma'])

                        oldax = plt.gca()
                        x,w,patches = oldax.hist(values[:,i], bins=nbins, edgecolor='grey', color='grey', histtype='stepfilled', alpha=0.2)
                        oldax.set_ylim(0, x.max())

                        newax = plt.gcf().add_axes(oldax.get_position(), sharex=oldax, frameon=False)
                        p.plot_marginal(i, ls='-', color='blue', linewidth=3)
                        newax.set_ylim(0, 1)

                        ylim = newax.get_ylim()
                        y = ylim[0] + 0.05*(ylim[1] - ylim[0])
                        center = m['median']
                        low1, high1 = m['1sigma']
                        print center, low1, high1
                        newax.errorbar(x=center, y=y,
                                xerr=np.transpose([[center - low1, high1 - center]]), 
                                color='blue', linewidth=2, marker='s')
                        oldax.set_yticks([])
                        #newax.set_yticks([])
                        newax.set_ylabel("Probability")
                        ylim = oldax.get_ylim()
                        newax.set_xlim(m['5sigma'])
                        oldax.set_xlim(m['5sigma'])
                        #plt.close()

                        for j in range(i):
                                plt.subplot(n_params, n_params, n_params * (j + 1) + i + 1)
                                p.plot_conditional(i, j, bins=20, cmap = plt.cm.gray_r)
                                for m in modes:
                                        plt.errorbar(x=m['mean'][i], y=m['mean'][j], xerr=m['sigma'][i], yerr=m['sigma'][j])
                                ax = plt.gca()
                                if j == i-1:
                                    plt.xlabel(parameters[i])
                                    plt.ylabel(parameters[j])
                                    [l.set_rotation(45) for l in ax.get_xticklabels()]
                                else:
                                    ax.set_xticklabels([])
                                    ax.set_yticklabels([])


                                plt.xlim([m['mean'][i]-5*m['sigma'][i],m['mean'][i]+5*m['sigma'][i]])
                                plt.ylim([m['mean'][j]-5*m['sigma'][j],m['mean'][j]+5*m['sigma'][j]])
                                #plt.savefig('cond_%s_%s.pdf' % (params[i], params[j]), bbox_tight=True)
                                #plt.close()

                plt.tight_layout()
                plt.savefig(prefix + 'marg.pdf')
                plt.savefig(prefix + 'marg.png')
                plt.close()
        else:
        	from matplotlib.backends.backend_pdf import PdfPages
        	print '1dimensional only. Set the D environment variable D=2 to force'
        	print '2d marginal plots.'
        	pp = PdfPages(prefix + 'marg1d.pdf')
        	
        	for i in range(n_params):
        		plt.figure(figsize=(5, 5))
        		plt.xlabel(parameters[i])
        		
        		m = s['marginals'][i]
        		plt.xlim(m['5sigma'])
        	
        		oldax = plt.gca()
        		x,w,patches = oldax.hist(values[:,i], bins=20, edgecolor='grey', color='grey', histtype='stepfilled', alpha=0.2)
        		oldax.set_ylim(0, x.max())
        	
        		newax = plt.gcf().add_axes(oldax.get_position(), sharex=oldax, frameon=False)
        		p.plot_marginal(i, ls='-', color='blue', linewidth=3)
        		newax.set_ylim(0, 1)
        	
        		ylim = newax.get_ylim()
        		y = ylim[0] + 0.05*(ylim[1] - ylim[0])
        		center = m['median']
        		low1, high1 = m['1sigma']
        		print center, low1, high1
        		newax.errorbar(x=center, y=y,
        			xerr=np.transpose([[center - low1, high1 - center]]), 
        			color='blue', linewidth=2, marker='s')
        		oldax.set_yticks([])
        		newax.set_ylabel("Probability")
        		ylim = oldax.get_ylim()
        		newax.set_xlim(m['5sigma'])
        		oldax.set_xlim(m['5sigma'])
        		plt.savefig(pp, format='pdf', bbox_inches='tight')
        		plt.close()
        	pp.close()
    
    
class PriorCollections(object):
    
    
    def __init__(self, prior_dict, parameter_order=[]):
        self.priors = prior_dict
        
    def prior_transform(self, cube, ndim, nparam):
        # will be given an array of values from 0 to 1 and transforms it 
        # according to the prior distribution
        
        for i in xrange(nparam):
            cube[i] = self.priors.values()[i](cube[i])

        
        
