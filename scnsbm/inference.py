from typing import Optional, Tuple, Sequence, Type, Union

import numpy as np
import pandas as pd
from anndata import AnnData
from scipy import sparse

from scanpy import logging as logg
from scanpy.tools._utils_clustering import rename_groups, restrict_adjacency
from .utils import get_graph_tool_from_adjacency

from sklearn.metrics import adjusted_mutual_info_score as ami

def prune_groups(groups, inverse=False):
    """
    Returns the index of informative levels after the nested_model has
    been run. It works by looking at level entropy and, moreover, checks if
    two consecutive levels have the same clustering
    """
    
    n_groups = groups.shape[1]
    
    mi_groups = np.array([ami(groups.iloc[:, x - 1], groups.iloc[:, x]) for x in range(1, n_groups)])
    
    if inverse:
        return groups.columns[np.where(mi_groups != 1)]
    
    return groups.columns[np.where(mi_groups == 1)]

def nested_model(
    adata: AnnData,
    sweep_iterations: int = 10000,
    max_iterations: int = 1000000,
    epsilon: float = 1e-3,
    equilibrate: bool = True,
    wait: int = 1000,
    nbreaks: int = 2,
    collect_marginals: bool = False,
    niter_collect: int = 10000,
    hierarchy_length: int = 10,
    deg_corr: bool = False,
    multiflip: bool = True,
    *,
    restrict_to: Optional[Tuple[str, Sequence[str]]] = None,
    random_seed: Optional[int] = None,
    key_added: str = 'nsbm',
    adjacency: Optional[sparse.spmatrix] = None,
    directed: bool = False,
    use_weights: bool = False,
    save_state: bool = False,
    prune: bool = False,
    return_low: bool = False,
    copy: bool = False
) -> Optional[AnnData]:
    """\
    Cluster cells into subgroups [Peixoto14]_.

    Cluster cells using the nested Stochastic Block Model [Peixoto14]_,
    a hierarchical version of Stochastic Block Model [Holland83]_, performing
    Bayesian inference on node groups. NSBM should circumvent classical
    limitations of SBM in detecting small groups in large graphs
    replacing the noninformative priors used by a hierarchy of priors
    and hyperpriors.

    This requires having ran :func:`~scanpy.pp.neighbors` or
    :func:`~scanpy.external.pp.bbknn` first.

    Parameters
    ----------
    adata
        The annotated data matrix.
    sweep_iterations
        Number of iterations to run mcmc_sweep.
        Higher values lead longer runtime.
    max_iterations
        Maximal number of iterations to be performed by the equilibrate step.
    epsilon
        Relative changes in entropy smaller than epsilon will
        not be considered as record-breaking.
    equilibrate
        Whether or not perform the mcmc_equilibrate step.
        Equilibration should always be performed. Note, also, that without
        equilibration it won't be possible to collect marginals.
    collect_marginals
        Whether or not collect node probability of belonging
        to a specific partition.
    niter_collect
        Number of iterations to force when collecting marginals. This will
        increase the precision when calculating probabilites
    wait
        Number of iterations to wait for a record-breaking event.
        Higher values result in longer computations. Set it to small values
        when performing quick tests.
    nbreaks
        Number of iteration intervals (of size `wait`) without
        record-breaking events necessary to stop the algorithm.
    hierarchy_length
        Initial length of the hierarchy. When large values are
        passed, the top-most levels will be uninformative as they
        will likely contain the very same groups. Increase this valus
        if a very large number of cells is analyzed (>100.000).
    deg_corr
        Whether to use degree correction in the minimization step. In many
        real world networks this is the case, although this doesn't seem
        the case for KNN graphs used in scanpy.
    multiflip
        Whether to perform MCMC sweep with multiple simultaneous moves to sample
        network partitions. It may result in slightly longer runtimes, but under
        the hood it allows for a more efficient space exploration.
    key_added
        `adata.obs` key under which to add the cluster labels.
    adjacency
        Sparse adjacency matrix of the graph, defaults to
        `adata.uns['neighbors']['connectivities']`.
    directed
        Whether to treat the graph as directed or undirected.
    use_weights
        If `True`, edge weights from the graph are used in the computation
        (placing more emphasis on stronger edges). Note that this
        increases computation times
    save_state
        Whether to keep the block model state saved for subsequent
        custom analysis with graph-tool. Use only for debug session, state
        is not (yet) supported for `sc.write` function
    prune
        Some high levels in hierarchy may contain the same information in terms of 
        cell assignments, even if they apparently have different group names. When this
        option is set to `True`, the function only returns informative levels.
        Note, however, that cell_marginals are still reported for all levels. Pruning
        does not rename group levels
    return_low
        Whether or not return nsbm_level_0 in adata.obs. This level usually contains
        so many groups that it cannot be plot anyway, but it may be useful for particular
        analysis. By default it is not returned
    copy
        Whether to copy `adata` or modify it inplace.
    random_seed
        Random number to be used as seed for graph-tool
    Returns
    -------
    `adata.obs[key_added]`
        Array of dim (number of samples) that stores the subgroup id
        (`'0'`, `'1'`, ...) for each cell. Multiple arrays will be
        added when `return_level` is set to `all`
    `adata.uns['nsbm']['params']`
        A dict with the values for the parameters `resolution`, `random_state`,
        and `n_iterations`.
    `adata.uns['nsbm']['stats']`
        A dict with the values returned by mcmc_sweep
    `adata.uns['nsbm']['cell_marginals']`
        A `np.ndarray` with cell probability of belonging to a specific group
    `adata.uns['nsbm']['state']`
        The NestedBlockModel state object
    """
    try:
        import graph_tool.all as gt
    except ImportError:
        raise ImportError(
            """Please install the graph-tool library either visiting

            https://git.skewed.de/count0/graph-tool/-/wikis/installation-instructions

            or by conda: `conda install -c conda-forge graph-tool`
            """
        )

    if random_seed:
        np.random.seed(random_seed)
        gt.seed_rng(random_seed)

    if collect_marginals:
        logg.warning('Collecting marginals has a large impact on running time')
        if not equilibrate:
            raise ValueError(
                "You can't collect marginals without MCMC equilibrate "
                "step. Either set `equlibrate` to `True` or "
                "`collect_marginals` to `False`"
            )

    start = logg.info('minimizing the nested Stochastic Block Model')
    adata = adata.copy() if copy else adata
    # are we clustering a user-provided graph or the default AnnData one?
    if adjacency is None:
        if 'neighbors' not in adata.uns:
            raise ValueError(
                'You need to run `pp.neighbors` first '
                'to compute a neighborhood graph.'
            )
        adjacency = adata.uns['neighbors']['connectivities']
    if restrict_to is not None:
        restrict_key, restrict_categories = restrict_to
        adjacency, restrict_indices = restrict_adjacency(
            adata,
            restrict_key,
            restrict_categories,
            adjacency,
        )
    # convert it to igraph
    g = get_graph_tool_from_adjacency(adjacency, directed=directed)

    if use_weights:
        # this is not ideal to me, possibly we may need to transform
        # weights. More tests needed.
        state = gt.minimize_nested_blockmodel_dl(g, deg_corr=deg_corr,
                                                 state_args=dict(recs=[g.ep.weight],
                                                 rec_types=['real-normal']))
    else:
        state = gt.minimize_nested_blockmodel_dl(g, deg_corr=deg_corr)
    logg.info('    done', time=start)
    bs = state.get_bs()
    if len(bs) < hierarchy_length:
        # increase hierarchy length up to the specified value
        # according to Tiago Peixoto 10 is reasonably large as number of
        # groups decays exponentially
        bs += [np.zeros(1)] * (hierarchy_length - len(bs))

    if use_weights:
        state = gt.NestedBlockState(g, bs, state_args=dict(recs=[g.ep.weight],
                                            rec_types=["real-normal"]), sampling=True)
    else:
        state = state.copy(bs=bs, sampling=True)

    # run the MCMC sweep step
    logg.info(f'running MCMC sweep step with {sweep_iterations} iterations')
    if multiflip:
        s_dS, s_nattempts, s_nmoves = state.multiflip_mcmc_sweep(niter=sweep_iterations)
    else:
        s_dS, s_nattempts, s_nmoves = state.mcmc_sweep(niter=sweep_iterations)
    logg.info('    done', time=start)

    # equilibrate the Markov chain
    if equilibrate:
        logg.info('running MCMC equilibration step')
        e_dS, e_nattempts, e_nmoves = gt.mcmc_equilibrate(state, wait=wait,
                                                          nbreaks=nbreaks,
                                                          epsilon=epsilon,
                                                          max_niter=max_iterations,
                                                          multiflip=multiflip,
                                                          mcmc_args=dict(niter=10)
                                                          )
        if collect_marginals:
            # we here only retain level_0 counts, until I can't figure out
            # how to propagate correctly counts to higher levels
            logg.info('    collecting marginals')
            group_marginals = [np.zeros(g.num_vertices() + 1) for s in state.get_levels()]
            def _collect_marginals(s):
                levels = s.get_levels()
                for l, sl in enumerate(levels):
                    group_marginals[l][sl.get_nonempty_B()] += 1

                # cell marginals need a global variable. It is a mess but this
                # is due to the way collect_vertex_marginals works.
                global cell_marginals
                try:
                    cell_marginals = [sl.collect_vertex_marginals(cell_marginals[l]) for l, sl in enumerate(levels)]
                except (NameError, ValueError):
                    # due to the way gt updates vertex marginals and the usage
                    # of global variables, our counter is persistent during the
                    # execution. For this we need to reinitialize it
                    cell_marginals = [None] * len(s.get_levels())

            gt.mcmc_equilibrate(state, wait=wait, nbreaks=nbreaks, epsilon=epsilon,
                                max_niter=max_iterations, multiflip=False,
                                force_niter=niter_collect, mcmc_args=dict(niter=10),
                                callback=_collect_marginals)
            logg.info('    done', time=start)

    # everything is in place, we need to fill all slots
    # first build an array with
    groups = np.zeros((g.num_vertices(), len(bs)), dtype=int)

    for x in range(len(bs)):
        # for each level, project labels to the vertex level
        # so that every cell has a name. Note that at this level
        # the labels are not necessarily consecutive
        groups[:, x] = state.project_partition(x, 0).get_array()

    groups = pd.DataFrame(groups).astype('category')

    # rename categories from 0 to n
    for c in groups.columns:
        new_cat_names = dict([(cx, u'%s' % cn) for cn, cx in enumerate(groups.loc[:, c].cat.categories)])
        groups.loc[:, c].cat.rename_categories(new_cat_names, inplace=True)

    if restrict_to is not None:
        groups.index = adata.obs[restrict_key].index
    else:
        groups.index = adata.obs_names

    # add column names
    groups.columns = ["%s_level_%d" % (key_added, level) for level in range(len(bs))]

    # remove any column with the same key
    keep_columns = [x for x in adata.obs.columns if not x.startswith('%s_level_' % key_added)]
    adata.obs = adata.obs.loc[:, keep_columns]
    # concatenate obs with new data, skipping level_0 which is usually
    # crap. In the future it may be useful to reintegrate it
    # we need it in this function anyway, to match groups with node marginals
    if return_low:
        adata.obs = pd.concat([adata.obs, groups], axis=1)
    else:
        adata.obs = pd.concat([adata.obs, groups.iloc[:, 1:]], axis=1)

    # add some unstructured info

    adata.uns['nsbm'] = {}
    adata.uns['nsbm']['stats'] = dict(
    sweep_dS=s_dS,
    sweep_nattempts=s_nattempts,
    sweep_nmoves=s_nmoves,
    level_entropy=np.array([state.level_entropy(x) for x in range(len(state.levels))]),
    modularity=np.array([gt.modularity(g, state.project_partition(x, 0))
                         for x in range(len((state.levels)))])
    )
    if equilibrate:
        adata.uns['nsbm']['stats'].update(dict(
        equlibrate_dS=e_dS,
        equlibrate_nattempts=e_nattempts,
        equlibrate_nmoves=e_nmoves,
        ))
        if collect_marginals:
            # since we have cell marginals we can also calculate
            # mean field entropy.
            adata.uns['nsbm']['stats']['mf_entropy'] = np.array([gt.mf_entropy(sl.g,
                                                                 cell_marginals[l])
                                                                 for l, sl in
                                                                 enumerate(state.get_levels())])

    if save_state:
        logg.warning("""It is not possible to dump on the disk `adata` objects'
         when `state` is saved into `adata.uns`.
         Remove it with `.pop` before saving data in .h5ad format""")
        adata.uns['nsbm']['state'] = state

    # now add marginal probabilities.

    if collect_marginals:
        # cell marginals will be a list of arrays with probabilities
        # of belonging to a specific group
        adata.uns['nsbm']['cell_marginals'] = {}
        # get counts for the lowest levels, cells by groups. This will be summed in the
        # parent levels, according to groupings
        l0_ngroups = state.get_levels()[0].get_nonempty_B()
        l0_counts = cell_marginals[0].get_2d_array(range(l0_ngroups))
        c0 = l0_counts.T
        adata.uns['nsbm']['cell_marginals'][0] = c0

        l0 = "%s_level_0" % key_added
        for nl, level in enumerate(groups.columns[1:]):
            cross_tab = pd.crosstab(groups.loc[:, l0], groups.loc[:, level])
            cl = np.zeros((c0.shape[0], cross_tab.shape[1]), dtype=c0.dtype)
            for x in range(cl.shape[1]):
                # sum counts of level_0 groups corresponding to
                # this group at current level
                cl[:, x] = c0[:, np.where(cross_tab.iloc[:, x] > 0)[0]].sum(axis=1)
            adata.uns['nsbm']['cell_marginals'][nl + 1] = cl     
#            adata.uns['nsbm']['cell_marginals'][nl + 1] = pd.DataFrame(cl, 
#                                                                       columns=cross_tab.columns,
#                                                                       index=groups.index)
        # refrain group marginals. We collected data in vector as long as
        # the number of cells, cut them into appropriate length data
        adata.uns['nsbm']['group_marginals'] = []
        for level_marginals in group_marginals:
            idx = np.where(level_marginals > 0)[0] + 1
            adata.uns['nsbm']['group_marginals'].append(level_marginals[:np.max(idx)])
        # delete global variables (safety?)
#        del cell_marginals

    # prune uninformative levels, if any
    if prune:
        to_remove = prune_groups(groups)
        logg.info(
            f'    Removing levels f{to_remove}'
        )
        adata.obs.drop(to_remove, axis='columns', inplace=True)
    
    # last step is recording some parameters used in this analysis
    adata.uns['nsbm']['params'] = dict(
        sweep_iterations=sweep_iterations,
        epsilon=epsilon,
        wait=wait,
        nbreaks=nbreaks,
        equilibrate=equilibrate,
        collect_marginals=collect_marginals,
        hierarchy_length=hierarchy_length,
        prune=prune,
    )


    logg.info(
        '    finished',
        time=start,
        deep=(
            f'found {state.get_levels()[1].get_nonempty_B()} clusters at level_1, and added\n'
            f'    {key_added!r}, the cluster labels (adata.obs, categorical)'
        ),
    )
    return adata if copy else None


def fast_model(
    adata: AnnData,
    max_iterations: int = 1000000,
    epsilon: float = 1e-3,
    wait: int = 1000,
    nbreaks: int = 2,
    collect_marginals: bool = False,
    niter_collect: int = 10000,
    hierarchy_length: int = 10,
    deg_corr: bool = False,
    multiflip: bool = True,
    *,
    restrict_to: Optional[Tuple[str, Sequence[str]]] = None,
    random_seed: Optional[int] = None,
    key_added: str = 'nsbm',
    adjacency: Optional[sparse.spmatrix] = None,
    directed: bool = False,
    use_weights: bool = False,
    save_state: bool = False,
    prune: bool = False,
    return_low: bool = False,
    copy: bool = False
) -> Optional[AnnData]:
    """\
    Cluster cells into subgroups [Peixoto14]_.

    Cluster cells using the nested Stochastic Block Model [Peixoto14]_,
    a hierarchical version of Stochastic Block Model [Holland83]_, performing
    Bayesian inference on node groups. NSBM should circumvent classical
    limitations of SBM in detecting small groups in large graphs
    replacing the noninformative priors used by a hierarchy of priors
    and hyperpriors. This function is a faster implementation of `nested_model`,
    it requires less memory and time, as it doesn't perform the initial minimization
    nor the mcmc_sweep step. In general it converges to similar results.

    This requires having ran :func:`~scanpy.pp.neighbors` or
    :func:`~scanpy.external.pp.bbknn` first.

    Parameters
    ----------
    adata
        The annotated data matrix.
    max_iterations
        Maximal number of iterations to be performed by the equilibrate step.
    epsilon
        Relative changes in entropy smaller than epsilon will
        not be considered as record-breaking.
    equilibrate
        Whether or not perform the mcmc_equilibrate step.
        Equilibration should always be performed. Note, also, that without
        equilibration it won't be possible to collect marginals.
    collect_marginals
        Whether or not collect node probability of belonging
        to a specific partition.
    niter_collect
        Number of iterations to force when collecting marginals. This will
        increase the precision when calculating probabilites
    wait
        Number of iterations to wait for a record-breaking event.
        Higher values result in longer computations. Set it to small values
        when performing quick tests.
    nbreaks
        Number of iteration intervals (of size `wait`) without
        record-breaking events necessary to stop the algorithm.
    hierarchy_length
        Initial length of the hierarchy. When large values are
        passed, the top-most levels will be uninformative as they
        will likely contain the very same groups. Increase this valus
        if a very large number of cells is analyzed (>100.000).
    deg_corr
        Whether to use degree correction in the minimization step. In many
        real world networks this is the case, although this doesn't seem
        the case for KNN graphs used in scanpy.
    multiflip
        Whether to perform MCMC sweep with multiple simultaneous moves to sample
        network partitions. It may result in slightly longer runtimes, but under
        the hood it allows for a more efficient space exploration.
    key_added
        `adata.obs` key under which to add the cluster labels.
    adjacency
        Sparse adjacency matrix of the graph, defaults to
        `adata.uns['neighbors']['connectivities']`.
    directed
        Whether to treat the graph as directed or undirected.
    use_weights
        If `True`, edge weights from the graph are used in the computation
        (placing more emphasis on stronger edges). Note that this
        increases computation times
    save_state
        Whether to keep the block model state saved for subsequent
        custom analysis with graph-tool. Use only for debug session, state
        is not (yet) supported for `sc.write` function
    prune
        Some high levels in hierarchy may contain the same information in terms of 
        cell assignments, even if they apparently have different group names. When this
        option is set to `True`, the function only returns informative levels.
        Note, however, that cell_marginals are still reported for all levels. Pruning
        does not rename group levels
    return_low
        Whether or not return nsbm_level_0 in adata.obs. This level usually contains
        so many groups that it cannot be plot anyway, but it may be useful for particular
        analysis. By default it is not returned
    copy
        Whether to copy `adata` or modify it inplace.
    random_seed
        Random number to be used as seed for graph-tool
    Returns
    -------
    `adata.obs[key_added]`
        Array of dim (number of samples) that stores the subgroup id
        (`'0'`, `'1'`, ...) for each cell. Multiple arrays will be
        added when `return_level` is set to `all`
    `adata.uns['nsbm']['params']`
        A dict with the values for the parameters `resolution`, `random_state`,
        and `n_iterations`.
    `adata.uns['nsbm']['stats']`
        A dict with the values returned by mcmc_sweep
    `adata.uns['nsbm']['cell_marginals']`
        A `np.ndarray` with cell probability of belonging to a specific group
    `adata.uns['nsbm']['state']`
        The NestedBlockModel state object
    """
    try:
        import graph_tool.all as gt
    except ImportError:
        raise ImportError(
            """Please install the graph-tool library either visiting

            https://git.skewed.de/count0/graph-tool/-/wikis/installation-instructions

            or by conda: `conda install -c conda-forge graph-tool`
            """
        )

    if random_seed:
        np.random.seed(random_seed)
        gt.seed_rng(random_seed)

    if collect_marginals:
        logg.warning('Collecting marginals has a large impact on running time')

    start = logg.info('minimizing the nested Stochastic Block Model')
    adata = adata.copy() if copy else adata
    # are we clustering a user-provided graph or the default AnnData one?
    if adjacency is None:
        if 'neighbors' not in adata.uns:
            raise ValueError(
                'You need to run `pp.neighbors` first '
                'to compute a neighborhood graph.'
            )
        adjacency = adata.uns['neighbors']['connectivities']
    if restrict_to is not None:
        restrict_key, restrict_categories = restrict_to
        adjacency, restrict_indices = restrict_adjacency(
            adata,
            restrict_key,
            restrict_categories,
            adjacency,
        )
    # convert it to igraph
    g = get_graph_tool_from_adjacency(adjacency, directed=directed)
    bs = [np.zeros(1)] * hierarchy_length

    if use_weights:
        # this is not ideal to me, possibly we may need to transform
        # weights. More tests needed.
        state = gt.NestedBlockState(g=g, bs=bs, sampling=True,
                                    state_args=dict(deg_corr=deg_corr,
                                                    recs=[g.ep.weight],
                                                    rec_types=['real-normal']
                                                    )
                                    )
    else:
        state = gt.NestedBlockState(g=g, bs=bs, sampling=True,
                                    state_args=dict(deg_corr=deg_corr)
                                    )

    # equilibrate the Markov chain
    logg.info('MCMC equilibration')
    e_dS, e_nattempts, e_nmoves = gt.mcmc_equilibrate(state, wait=wait,
                                                          nbreaks=nbreaks,
                                                          epsilon=epsilon,
                                                          max_niter=max_iterations,
                                                          multiflip=multiflip,
                                                          mcmc_args=dict(niter=10)
                                                          )
    logg.info('    done', time=start)
    if collect_marginals:
        # we here only retain level_0 counts, until I can't figure out
        # how to propagate correctly counts to higher levels
        logg.info('    collecting marginals')
        group_marginals = [np.zeros(g.num_vertices() + 1) for s in state.get_levels()]
        def _collect_marginals(s):
            levels = s.get_levels()
            for l, sl in enumerate(levels):
                group_marginals[l][sl.get_nonempty_B()] += 1

            # cell marginals need a global variable. It is a mess but this
            # is due to the way collect_vertex_marginals works.
            global cell_marginals
            try:
                cell_marginals = [sl.collect_vertex_marginals(cell_marginals[l]) for l, sl in enumerate(levels)]
            except (NameError, ValueError):
                # due to the way gt updates vertex marginals and the usage
                # of global variables, our counter is persistent during the
                # execution. For this we need to reinitialize it
                cell_marginals = [None] * len(s.get_levels())

        gt.mcmc_equilibrate(state, wait=wait, nbreaks=nbreaks, epsilon=epsilon,
                            max_niter=max_iterations, multiflip=False,
                            force_niter=niter_collect, mcmc_args=dict(niter=10),
                            callback=_collect_marginals)
        logg.info('    done', time=start)

    # everything is in place, we need to fill all slots
    # first build an array with
    groups = np.zeros((g.num_vertices(), len(bs)), dtype=int)

    for x in range(len(bs)):
        # for each level, project labels to the vertex level
        # so that every cell has a name. Note that at this level
        # the labels are not necessarily consecutive
        groups[:, x] = state.project_partition(x, 0).get_array()

    groups = pd.DataFrame(groups).astype('category')

    # rename categories from 0 to n
    for c in groups.columns:
        new_cat_names = dict([(cx, u'%s' % cn) for cn, cx in enumerate(groups.loc[:, c].cat.categories)])
        groups.loc[:, c].cat.rename_categories(new_cat_names, inplace=True)

    if restrict_to is not None:
        groups.index = adata.obs[restrict_key].index
    else:
        groups.index = adata.obs_names

    # add column names
    groups.columns = ["%s_level_%d" % (key_added, level) for level in range(len(bs))]

    # remove any column with the same key
    keep_columns = [x for x in adata.obs.columns if not x.startswith('%s_level_' % key_added)]
    adata.obs = adata.obs.loc[:, keep_columns]
    # concatenate obs with new data, skipping level_0 which is usually
    # crap. In the future it may be useful to reintegrate it
    # we need it in this function anyway, to match groups with node marginals
    if return_low:
        adata.obs = pd.concat([adata.obs, groups], axis=1)
    else:
        adata.obs = pd.concat([adata.obs, groups.iloc[:, 1:]], axis=1)

    # add some unstructured info

    adata.uns['nsbm'] = {}
    adata.uns['nsbm']['stats'] = dict(
    dS=e_dS,
    nattempts=e_nattempts,
    nmoves=e_nmoves,
    level_entropy=np.array([state.level_entropy(x) for x in range(len(state.levels))]),
    modularity=np.array([gt.modularity(g, state.project_partition(x, 0))
                         for x in range(len((state.levels)))])
    )
    if collect_marginals:
        # since we have cell marginals we can also calculate
        # mean field entropy.
        adata.uns['nsbm']['stats']['mf_entropy'] = np.array([gt.mf_entropy(sl.g,
                                                             cell_marginals[l])
                                                             for l, sl in
                                                             enumerate(state.get_levels())])

    if save_state:
        logg.warning("""It is not possible to dump on the disk `adata` objects'
         when `state` is saved into `adata.uns`.
         Remove it with `.pop` before saving data in .h5ad format""")
        adata.uns['nsbm']['state'] = state

    # now add marginal probabilities.

    if collect_marginals:
        # cell marginals will be a list of arrays with probabilities
        # of belonging to a specific group
        adata.uns['nsbm']['cell_marginals'] = {}
        # get counts for the lowest levels, cells by groups. This will be summed in the
        # parent levels, according to groupings
        l0_ngroups = state.get_levels()[0].get_nonempty_B()
        l0_counts = cell_marginals[0].get_2d_array(range(l0_ngroups))
        c0 = l0_counts.T
        adata.uns['nsbm']['cell_marginals'][0] = c0

        l0 = "%s_level_0" % key_added
        for nl, level in enumerate(groups.columns[1:]):
            cross_tab = pd.crosstab(groups.loc[:, l0], groups.loc[:, level])
            cl = np.zeros((c0.shape[0], cross_tab.shape[1]), dtype=c0.dtype)
            for x in range(cl.shape[1]):
                # sum counts of level_0 groups corresponding to
                # this group at current level
                cl[:, x] = c0[:, np.where(cross_tab.iloc[:, x] > 0)[0]].sum(axis=1)
            adata.uns['nsbm']['cell_marginals'][nl + 1] = cl     
#            adata.uns['nsbm']['cell_marginals'][nl + 1] = pd.DataFrame(cl, 
#                                                                       columns=cross_tab.columns,
#                                                                       index=groups.index)
        # refrain group marginals. We collected data in vector as long as
        # the number of cells, cut them into appropriate length data
        adata.uns['nsbm']['group_marginals'] = []
        for level_marginals in group_marginals:
            idx = np.where(level_marginals > 0)[0] + 1
            adata.uns['nsbm']['group_marginals'].append(level_marginals[:np.max(idx)])
        # delete global variables (safety?)
#        del cell_marginals

    # prune uninformative levels, if any
    if prune:
        to_remove = prune_groups(groups)
        logg.info(
            f'    Removing levels f{to_remove}'
        )
        adata.obs.drop(to_remove, axis='columns', inplace=True)
    
    # last step is recording some parameters used in this analysis
    adata.uns['nsbm']['params'] = dict(
        epsilon=epsilon,
        wait=wait,
        nbreaks=nbreaks,
        collect_marginals=collect_marginals,
        hierarchy_length=hierarchy_length,
        prune=prune,
    )


    logg.info(
        '    finished',
        time=start,
        deep=(
            f'found {state.get_levels()[1].get_nonempty_B()} clusters at level_1, and added\n'
            f'    {key_added!r}, the cluster labels (adata.obs, categorical)'
        ),
    )
    return adata if copy else None

