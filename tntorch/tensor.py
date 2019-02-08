import numpy as np
import torch
import tntorch as tn
torch.set_default_dtype(torch.float64)
import time


def _full_rank_tt(data):  # Naive TT formatting, don't even attempt to compress
    shape = data.shape
    result = []
    N = data.dim()
    data = torch.Tensor(data) if type(data) is not torch.Tensor else data
    device = data.device
    resh = torch.reshape(data, [shape[0], -1])
    for n in range(1, N):
        if resh.shape[0] < resh.shape[1]:
            result.append(torch.reshape(torch.eye(resh.shape[0]).to(device), [resh.shape[0] // shape[n - 1],
                                                                       shape[n - 1], resh.shape[0]]))
            resh = torch.reshape(resh, (resh.shape[0] * shape[n], resh.shape[1] // shape[n]))
        else:
            result.append(torch.reshape(resh, [resh.shape[0] // shape[n - 1],
                                                   shape[n - 1], resh.shape[1]]))
            resh = torch.reshape(torch.eye(resh.shape[1]).to(device), (resh.shape[1] * shape[n], resh.shape[1] // shape[n]))
    result.append(torch.reshape(resh, [resh.shape[0] // shape[N - 1], shape[N - 1], 1]))
    return result


class Tensor(object):

    """
    Class for all tensor networks. Currently supported: `tensor train (TT) <https://epubs.siam.org/doi/pdf/10.1137/090752286>`_, `CANDECOMP/PARAFAC (CP) <https://epubs.siam.org/doi/pdf/10.1137/07070111X>`_, `Tucker <https://epubs.siam.org/doi/pdf/10.1137/S0895479898346995>`_, and hybrid formats.

    Internal representation: an ND tensor has N cores, with each core following one of four options:

    - Size :math:`R_{n-1} \\times I_n \\times R_n` (standard TT core)
    - Size :math:`R_{n-1} \\times S_n \\times R_n` (TT-Tucker core), accompanied by an :math:`I_n \\times S_n` factor matrix
    - Size :math:`I_n \\times R` (CP factor matrix)
    - Size :math:`S_n \\times R_n` (CP-Tucker core), accompanied by an :math:`I_n \\times S_n` factor matrix
    """

    def __init__(self, data, Us=None, idxs=None, device=None,
                 ranks_cp=None, ranks_tucker=None, ranks_tt=None, eps=None,
                 max_iter=25, tol=1e-4, verbose=False):

        """
        Constructor that can either:
        - Decompose an uncompressed tensor
        - Use an explicit list of tensor cores (and optionally, factors)

        See `this notebook <https://github.com/rballester/tntorch/blob/master/tutorials/decompositions.ipynb>`_ for examples of use.

        :param data: a NumPy ndarray, PyTorch tensor, or a list of cores (which can represent either CP factors or TT cores)
        :param Us: optional list of Tucker factors
        :param idxs: annotate maskable tensors (advanced users)
        :param device: PyTorch device
        :param ranks_cp: an integer (or list)
        :param ranks_tucker: an integer (or list)
        :param ranks_tt: an integer (or list)
        :param eps:
        :param max_iter:
        :param tol:
        :param verbose:

        :return: a tensor
        """

        if isinstance(data, (list, tuple)):
            if not all([2 <= d.dim() <= 3 for d in data]):
                raise ValueError('All tensor cores must have 2 (for CP) or 3 (for TT) dimensions')
            for n in range(len(data)-1):
                if (data[n+1].dim() == 3 and data[n].shape[-1] != data[n+1].shape[0]) or (data[n+1].dim() == 2 and data[n].shape[-1] != data[n+1].shape[1]):
                    raise ValueError('Core ranks do not match')
            self.cores = data
            N = len(data)
        else:
            if isinstance(data, np.ndarray):
                data = torch.Tensor(data, device=device)
            elif isinstance(data, torch.Tensor):
                data = data.to(device)
            else:
                raise ValueError('A tntorch.Tensor may be built either from a list of cores, one NumPy ndarray, or one PyTorch tensor')
            N = data.dim()
        if Us is None:
            Us = [None]*N
        self.Us = Us
        if isinstance(data, torch.Tensor):
            if data.dim() == 0:
                data = data*torch.ones(1, device=device)
            if ranks_cp is not None:  # Compute CP from full tensor: CP-ALS
                if ranks_tt is not None:
                    raise ValueError('ALS for CP-TT is not yet supported')
                assert not hasattr(ranks_cp, '__len__')
                start = time.time()
                if verbose:
                    print('ALS', end='')
                if ranks_tucker is not None:  # CP on Tucker's core
                    self.cores = _full_rank_tt(data)
                    self.round_tucker(rmax=ranks_tucker)
                    data = self.tucker_core()
                    data_norm = tn.norm(data)
                    self.cores = [torch.randn(sh, ranks_cp, device=device) for sh in data.shape]
                else:  # We initialize CP factor to HOSVD
                    data_norm = torch.norm(data)
                    self.cores = []
                    for n in range(data.dim()):
                        gram = tn.unfolding(data, n)
                        gram = gram.matmul(gram.t())
                        eigvals, eigvecs = torch.symeig(gram, eigenvectors=True)
                        # Sort eigenvectors in decreasing importance
                        reverse = np.arange(len(eigvals)-1, -1, -1)  # Negative steps not yet supported in PyTorch
                        idx = np.argsort(eigvals)[reverse[:ranks_cp]]
                        self.cores.append(eigvecs[:, idx])
                if verbose:
                    print(' -- initialization time =', time.time() - start)
                grams = [None] + [self.cores[n].t().matmul(self.cores[n]) for n in range(1, self.dim())]
                errors = []
                converged = False
                for iter in range(max_iter):
                    for n in range(self.dim()):
                        khatri = torch.ones(1, ranks_cp, device=device)
                        prod = torch.ones(ranks_cp, ranks_cp, device=device)
                        for m in range(self.dim()-1, -1, -1):
                            if m != n:
                                prod *= grams[m]
                                khatri = torch.reshape(torch.einsum('ir,jr->ijr', (self.cores[m], khatri)), [-1, ranks_cp])
                        unfolding = tn.unfolding(data, n)
                        self.cores[n] = torch.gesv(unfolding.matmul(khatri).t(), prod)[0].t()
                        grams[n] = self.cores[n].t().matmul(self.cores[n])
                    errors.append(torch.norm(data - tn.Tensor(self.cores).torch()) / data_norm)
                    if len(errors) >= 2 and errors[-2] - errors[-1] < tol:
                        converged = True
                    if verbose:
                        print('iter: {: <{}} | eps: '.format(iter, len('{}'.format(max_iter))), end='')
                        print('{:.8f}'.format(errors[-1]), end='')
                        print(' | total time: {:9.4f}'.format(time.time() - start), end='')
                        if converged:
                            print(' <- converged (tol={})'.format(tol))
                        elif iter == max_iter-1:
                            print(' <- max_iter was reached: {}'.format(max_iter))
                        else:
                            print()
                    if converged:
                        break
            else:
                self.cores = _full_rank_tt(data)
                self.Us = [None]*data.dim()
                if ranks_tucker is not None:
                    self.round_tucker(rmax=ranks_tucker)
                if ranks_tt is not None:
                    self.round_tt(rmax=ranks_tt)
        # Check factor shapes
        for n in range(self.dim()):
            if Us[n] is None:
                continue
            assert Us[n].dim() == 2
            assert self.cores[n].shape[-2] == Us[n].shape[1]
        if idxs is None:
            idxs = [torch.arange(sh, device=device) for sh in self.shape]
        self.idxs = idxs
        if eps is not None:  # TT-SVD (or TT-EIG) algorithm
            if ranks_cp is not None or ranks_tucker is not None or ranks_tt is not None:
                raise ValueError('Specify eps or ranks, but not both')
            self.round(eps)

    """
    Arithmetic operations
    """

    def __add__(self, other):
        if not isinstance(other, Tensor):
            factor = other
            other = Tensor([torch.ones([1, self.shape[n], 1]) for n in range(self.dim())])
            other.cores[0].data *= factor
        if self.dim() == 1:  # Special case
            return Tensor([self.decompress_tucker_factors().cores[0] + other.decompress_tucker_factors().cores[0]])
        this, other = _broadcast(self, other)
        cores = []
        Us = []
        for n in range(this.dim()):
            core1 = this.cores[n]
            core2 = other.cores[n]
            # CP + CP -> CP, other combinations -> TT
            if core1.dim() == 2 and core2.dim() == 2:
                core1 = core1[None, :, :]
                core2 = core2[None, :, :]
            else:
                core1 = self._cp_to_tt(core1)
                core2 = self._cp_to_tt(core2)
            if this.Us[n] is not None and other.Us[n] is not None:
                # if core1.shape[1] + core2.shape[1] >= self.Us[n] and core1.shape[1] + core2.shape[1] >= self.Us[n]
                slice1 = torch.cat([core1, torch.zeros([core2.shape[0], core1.shape[1], core1.shape[2]])], dim=0)
                slice1 = torch.cat([slice1, torch.zeros(core1.shape[0]+core2.shape[0], core1.shape[1], core2.shape[2])], dim=2)
                slice2 = torch.cat([torch.zeros([core1.shape[0], core2.shape[1], core2.shape[2]]), core2], dim=0)
                slice2 = torch.cat([torch.zeros(core1.shape[0]+core2.shape[0], core2.shape[1], core1.shape[2]), slice2], dim=2)
                c = torch.cat([slice1, slice2], dim=1)
                cores.append(c)
                Us.append(torch.cat((self.Us[n], other.Us[n]), dim=1))
                continue
            if this.Us[n] is not None:
                core1 = torch.einsum('ijk,aj->iak', (core1, self.Us[n]))
            if other.Us[n] is not None:
                core2 = torch.einsum('ijk,aj->iak', (core2, other.Us[n]))
            column1 = torch.cat([core1, torch.zeros([core2.shape[0], this.shape[n], core1.shape[2]])], dim=0)
            column2 = torch.cat([torch.zeros([core1.shape[0], this.shape[n], core2.shape[2]]), core2], dim=0)
            c = torch.cat([column1, column2], dim=2)
            cores.append(c)
            Us.append(None)

        # First core should have first size 1 (if it's TT)
        if not (this.cores[0].dim() == 2 and other.cores[0].dim() == 2):
            cores[0] = torch.sum(cores[0], dim=0, keepdim=True)
        # Similarly for the last core and last size
        if not (this.cores[-1].dim() == 2 and other.cores[-1].dim() == 2):
            cores[-1] = torch.sum(cores[-1], dim=2, keepdim=True)

        # Set up cores that should be CP cores
        for n in range(0, this.dim()):
            if this.cores[n].dim() == 2 and other.cores[n].dim() == 2:
                cores[n] = torch.sum(cores[n], dim=0, keepdim=False)

        return Tensor(cores, Us=Us)

    def __radd__(self, other):
        if other is None:
            return self
        return self + other

    def __sub__(self, other):
        return self + -1*other

    def __rsub__(self, other):
        return -1*self + other

    def __neg__(self):
        return -1*self

    def __mul__(self, other):
        if not isinstance(other, Tensor):  # A scalar
            result = self.clone()
            result.cores[0].data *= other
            return result
        this, other = _broadcast(self, other)
        cores = []
        Us = []
        for n in range(this.dim()):
            core1 = this.cores[n]
            core2 = other.cores[n]
            # CP + CP -> CP, other combinations -> TT
            if core1.dim() == 2 and core2.dim() == 2:
                core1 = core1[None, :, :]
                core2 = core2[None, :, :]
            else:
                core1 = this._cp_to_tt(core1)
                core2 = this._cp_to_tt(core2)
            # We do the product core along 3 axes, unless it would blow up
            if this.Us[n] is not None and other.Us[n] is not None and this.cores[n].shape[1]*other.cores[n].shape[1] < this.shape[n]:
                cores.append(torch.reshape(torch.einsum('ijk,abc->iajbkc', (core1, core2)),
                                           (core1.shape[0]*core2.shape[0],
                                            core1.shape[1]*core2.shape[1],
                                            core1.shape[2]*core2.shape[2])))
                Us.append(torch.reshape(torch.einsum('ij,ik->ijk', (this.Us[n], other.Us[n])),
                         (this.Us[n].shape[0], -1)))
            else:  # Decompress spatially, then do normal TT-TT slice-wise kronecker product
                if this.Us[n] is not None:
                    core1 = torch.einsum('ijk,aj->iak', (core1, this.Us[n]))
                if other.Us[n] is not None:
                    core2 = torch.einsum('ijk,aj->iak', (core2, other.Us[n]))
                cores.append(_core_kron(core1, core2))
                Us.append(None)
            if this.cores[n].dim() == 2 and other.cores[n].dim() == 2:
                cores[-1] = cores[-1][0, :, :]
        return tn.Tensor(cores, Us=Us)

    """
    Boolean logic
    """

    def __rmul__(self, other):
        return self * other

    def __truediv__(self, other):
        return self * (1./other)

    def __invert__(self):
        return 1 - self

    def __and__(self, other):
        return self*other

    def __or__(self, other):
        return self+other - self*other

    def __xor__(self, other):
        return self+other - 2*self*other

    def __eq__(self, other):
        return (self-other).norm() <= 1e-14

    def __ne__(self, other):
        return not self == other

    """
    Shapes and ranks
    """

    @property
    def shape(self):
        """
        Returns the shape of this tensor.

        :return: a PyTorch shape object
        """

        shape = []
        for n in range(self.dim()):
            if self.Us[n] is None:
                shape.append(self.cores[n].shape[-2])
            else:
                shape.append(self.Us[n].shape[0])
        return torch.Size(shape)

    @property
    def ranks_tt(self):
        if self.cores[0].dim() == 2:
            first = self.cores[0].shape[1]
        else:
            first = self.cores[0].shape[0]
        return np.array([first] + [c.shape[-1] for c in self.cores])

    @ranks_tt.setter
    def ranks_tt(self, value):
        self.round_tt(rmax=value)

    @property
    def ranks_tucker(self):
        return np.array([c.shape[-2] for c in self.cores])

    @ranks_tucker.setter
    def ranks_tucker(self, value):
        self.round_tucker(rmax=value)

    def dim(self):
        """
        Returns the number of dimensions of this tensor.

        :return: an int
        """

        return len(self.cores)

    def size(self):
        """
        Alias for :meth:`shape` (as PyTorch does)
        """

        return self.shape

    def __repr__(self):

        format = []
        if any([c.dim() == 3 for c in self.cores]):
            format.append('TT')
        if any([c.dim() == 2 for c in self.cores]):
            format.append('CP')
        if any([U is not None for U in self.Us]):
            format.append('Tucker')
        format = '-'.join(format)
        s = '{}D {} tensor:\n'.format(self.dim(), format)
        s += '\n'
        ttr = self.ranks_tt
        tuckerr = self.ranks_tucker

        if any([U is not None for U in self.Us]):

            # Shape
            row = [' ']*(4*self.dim()-1)
            shape = self.shape
            for n in range(self.dim()):
                if self.Us[n] is None:
                    continue
                lenn = len('{}'.format(shape[n]))
                row[n*4-lenn//2+2:n*4-lenn//2+lenn+2] = '{}'.format(shape[n])
            s += ''.join(row)
            s += '\n'

        # Tucker ranks
        row = [' ']*(4*self.dim()-1)
        for n in range(self.dim()):
            if self.Us[n] is None:
                lenr = len('{}'.format(tuckerr[n]))
                row[n*4-lenr//2+2:n*4-lenr//2+lenr+2] = '{}'.format(tuckerr[n])
            else:
                row[n*4+2:n*4+3] = '|'
        s += ''.join(row)
        s += '\n'

        row = [' ']*(4*self.dim()-1)
        for n in range(self.dim()):
            if self.Us[n] is None:
                row[n*4+2:n*4+3] = '|'
            else:
                lenr = len('{}'.format(tuckerr[n]))
                row[n*4-lenr//2+2:n*4-lenr//2+lenr+2] = '{}'.format(tuckerr[n])
        s += ''.join(row)
        s += '\n'

        # Nodes
        row = [' ']*(4*self.dim()-1)
        for n in range(self.dim()):
            if self.cores[n].dim() == 2:
                nodestr = '<{}>'.format(n)
            else:
                nodestr = '({})'.format(n)
            lenn = len(nodestr)
            row[(n+1)*4-(lenn-1)//2:(n+1)*4-(lenn-1)//2+lenn] = nodestr
        s += ''.join(row[2:])
        s += '\n'

        # TT rank bars
        s += ' / \\'*self.dim()
        s += '\n'

        # Bottom: TT/CP ranks
        row = [' ']*(4*self.dim())
        for n in range(self.dim()+1):
            lenr = len('{}'.format(ttr[n]))
            row[n*4:n*4+lenr] = '{}'.format(ttr[n])
        s += ''.join(row)
        s += '\n'

        return s

    """
    Decompression
    """

    def _process_key(self, key):
        if not hasattr(key, '__len__'):
            key = (key,)
        fancy = False
        if isinstance(key, torch.Tensor):
            key = key.detach().numpy()
        if any([not np.isscalar(k) for k in key]):  # Fancy
            key = list(key)
            fancy = True
        if isinstance(key, tuple):
            key = list(key)
        elif not fancy:
            key = [key]

        # Process ellipsis, if any
        nonecount = sum(1 for k in key if k is None)
        for i in range(len(key)):
            if key[i] is Ellipsis:
                key = key[:i] + [slice(None)] * (self.dim() - (len(key) - nonecount) + 1) + key[i + 1:]
                break
        if any([k is Ellipsis for k in key]):
            raise IndexError("Only one ellipsis is allowed, at most")
        if self.dim() - (len(key) - nonecount) < 0:
            raise IndexError("Too many index entries")

        # Fill remaining unspecified dimensions with slice(None)
        key = key + [slice(None)] * (self.dim() - (len(key) - nonecount))
        return key

    def __getitem__(self, key):
        """
        NumPy-style indexing for compressed tensors. There are 5 accessors supported: slices, index arrays, integers,
        None, or another Tensor (selection via binary indexing)

        - Index arrays can be lists, tuples, or vectors
        - All index arrays must have the same length P
        - In NumPy, index arrays and slices can be interleaved. We do not admit that, as it requires expensive transpose operations

        """

        # Preprocessing
        if isinstance(key, Tensor):
            if torch.abs(tn.sum(key)-1) > 1e-8:
                raise ValueError("When indexing via a mask tensor, that mask should have exactly 1 accepting string")
            s = tn.accepted_inputs(key)[0]
            slicing = []
            for n in range(self.dim()):
                idx = self.idxs[n].long()
                idx[idx > 1] = 1
                idx = np.where(idx == s[n])[0]
                sl = slice(idx[0], idx[-1]+1)
                lenidx = len(idx)
                if lenidx == 1:
                    sl = idx.item()
                slicing.append(sl)
            return self[slicing]

        if isinstance(key, torch.Tensor):
            key = np.array(key, dtype=np.int)
        if isinstance(key, np.ndarray) and key.ndim == 2:
            key = [key[:, col] for col in range(key.shape[1])]
        key = self._process_key(key)
        last_mode = None
        factors = {'int': None, 'index': None, 'index_done': False}
        cores = []
        Us = []
        counter = 0

        def join_cores(c1, c2):
            if c1.dim() == 1 and c2.dim() == 2:
                return torch.einsum('i,ai->ai', (c1, c2))
            elif c1.dim() == 2 and c2.dim() == 2:
                return torch.einsum('ij,aj->iaj', (c1, c2))
            elif c1.dim() == 1 and c2.dim() == 3:
                return torch.einsum('i,iaj->iaj', (c1, c2))
            elif c1.dim() == 2 and c2.dim() == 3:
                return torch.einsum('ij,jak->iak', (c1, c2))
            else:
                raise ValueError

        def insert_core(factors, core=None, key=None, U=None):
            if factors['index'] is not None:
                if factors['int'] is not None:
                    factors['index'] = join_cores(factors['int'], factors['index'])
                    factors['int'] = None
                cores.append(factors['index'])
                Us.append(None)
                factors['index'] = None
                factors['index_done'] = True
            if core is not None:
                if factors['int'] is not None:  # There is a previous 1D/2D core (CP/Tucker) from an integer slicing
                    if U is None:
                        cores.append(join_cores(factors['int'], core[..., key, :]))
                        Us.append(None)
                    else:
                        cores.append(join_cores(factors['int'], core))
                        Us.append(U[key, :])
                    factors['int'] = None
                else:  # Easiest case
                    if U is None:
                        cores.append(core[..., key, :])
                        Us.append(None)
                    else:
                        cores.append(core)
                        Us.append(U[key, :])

        def get_key(counter, key):
            if self.Us[counter] is None:
                return self.cores[counter][..., key, :]
            else:
                sl = self.Us[counter][key, :]
                if sl.dim() == 1:  # key is an int
                    if self.cores[counter].dim() == 3:
                        return torch.einsum('ijk,j->ik', (self.cores[counter], sl))
                    else:
                        return torch.einsum('ji,j->i', (self.cores[counter], sl))
                else:
                    if self.cores[counter].dim() == 3:
                        return torch.einsum('ijk,aj->iak', (self.cores[counter], sl))
                    else:
                        return torch.einsum('ji,aj->ai', (self.cores[counter], sl))

        for i in range(len(key)):
            if hasattr(key[i], '__len__'):
                this_mode = 'index'
            elif key[i] is None:
                this_mode = 'none'
            elif isinstance(key[i], (int, np.integer)):
                this_mode = 'int'
            elif isinstance(key[i], slice):
                this_mode = 'slice'
            else:
                raise IndexError

            if this_mode == 'none':
                insert_core(factors, torch.eye(self.ranks_tt[counter].item())[:, None, :], key=slice(None), U=None)
            elif this_mode == 'slice':
                insert_core(factors, self.cores[counter], key=key[i], U=self.Us[counter])
                counter += 1
            elif this_mode == 'index':
                if factors['index_done']:
                    raise IndexError("All index arrays must appear contiguously")
                if factors['index'] is None:
                    factors['index'] = get_key(counter, key[i])
                else:
                    if factors['index'].shape[-2] != len(key[i]):
                        raise ValueError("Index arrays must have the same length")
                    a1 = factors['index']
                    a2 = get_key(counter, key[i])
                    if a1.dim() == 2 and a2.dim() == 2:
                        factors['index'] = torch.einsum('ai,ai->ai', (a1, a2))
                    elif a1.dim() == 2 and a2.dim() == 3:
                        factors['index'] = torch.einsum('ai,iaj->iaj', (a1, a2))
                    elif a1.dim() == 3 and a2.dim() == 2:
                        factors['index'] = torch.einsum('iaj,aj->iaj', (a1, a2))
                    elif a1.dim() == 3 and a2.dim() == 3:
                        # Until https://github.com/pytorch/pytorch/issues/10661 is fully resolved  # TODO check efficiency for other cases
                        factors['index'] = torch.sum(a1[:, :, :, None]*a2.permute(1, 0, 2)[None, :, :, :], dim=2)
                        # factors['index'] = torch.einsum('iaj,jak->iak', (a1, a2))
                counter += 1
            elif this_mode == 'int':
                if last_mode == 'index':
                    insert_core(factors)
                if factors['int'] is None:
                    factors['int'] = get_key(counter, key[i])
                else:
                    c1 = factors['int']
                    c2 = get_key(counter, key[i])
                    if c1.dim() == 1 and c2.dim() == 1:
                        factors['int'] = torch.einsum('i,i->i', (c1, c2))
                    elif c1.dim() == 1 and c2.dim() == 2:
                        factors['int'] = torch.einsum('i,ij->ij', (c1, c2))
                    elif c1.dim() == 2 and c2.dim() == 1:
                        factors['int'] = torch.einsum('ij,j->ij', (c1, c2))
                    elif c1.dim() == 2 and c2.dim() == 2:
                        factors['int'] = torch.einsum('ij,jk->ik', (c1, c2))
                counter += 1
            last_mode = this_mode

        # At the end: handle possibly pending factors
        if last_mode == 'index':
            insert_core(factors, core=None, key=None, U=None)
        elif last_mode == 'int':
            if len(cores) > 0:  # We return a tensor: absorb existing cores with int factor
                if cores[-1].dim() == 2 and factors['int'].dim() == 1:
                    cores[-1] = torch.einsum('ai,i->ai', (cores[-1], factors['int']))
                elif cores[-1].dim() == 2 and factors['int'].dim() == 2:
                    cores[-1] = torch.einsum('ai,ij->iaj', (cores[-1], factors['int']))
                elif cores[-1].dim() == 3 and factors['int'].dim() == 1:
                    cores[-1] = torch.einsum('iaj,j->ai', (cores[-1], factors['int']))
                elif cores[-1].dim() == 3 and factors['int'].dim() == 2:
                    cores[-1] = torch.einsum('iaj,jk->iak', (cores[-1], factors['int']))
            else:  # We return a scalar
                if factors['int'].numel() > 1:
                    return torch.sum(factors['int'])
                return torch.squeeze(factors['int'])
        return tn.Tensor(cores, Us=Us)

    def __setitem__(self, key, value):  # TODO not fully working yet
        key = self._process_key(key)
        scalar = False
        if isinstance(value, np.ndarray):
            value = tn.Tensor(torch.Tensor(value))
        elif isinstance(value, torch.Tensor):
            if value.dim() == 0:
                value = value.item()
                scalar = True
                # value = value*torch.ones(self.shape)
            else:
                value = tn.Tensor(value)
        elif isinstance(value, tn.Tensor):
            pass
        else:  # It's a scalar
            scalar = True

        subtract_cores = []
        add_cores = []
        for i in range(len(key)):
            if not isinstance(key[i], slice) and not hasattr(key[i], '__len__'):
                key[i] = slice(key[i], key[i]+1)
            chunk = self.cores[i][..., key[i], :]
            subtract_core = torch.zeros_like(self.cores[i])
            subtract_core[..., key[i], :] += chunk
            subtract_cores.append(subtract_core)
            if scalar:
                if self.cores[i].dim() == 3:
                    add_core = torch.zeros(1, self.shape[i], 1)
                else:
                    add_core = torch.zeros(self.shape[i], 1)
                add_core[..., key[i], :] += 1
                if i == 0:
                    add_core *= value
            else:
                if chunk.shape[1] != value.shape[i]:
                    raise ValueError('{}-th dimension mismatch in tensor assignment: {} (lhs) != {} (rhs)'.format(i, chunk.shape[1], value.shape[i]))
                if self.cores[i].dim() == 3:
                    add_core = torch.zeros(value.cores[i].shape[0], self.shape[i], value.cores[i].shape[2])
                else:
                    add_core = torch.zeros(self.shape[i], value.cores[i].shape[1])
                add_core[..., key[i], :] += value.cores[i]
            add_cores.append(add_core)
        # print(tn.Tensor(subtract_cores), tn.Tensor(add_cores))
        result = self - tn.Tensor(subtract_cores) + tn.Tensor(add_cores)
        self.__init__(result.cores, result.Us, self.idxs)

    def tucker_core(self):
        """
        If this is a Tucker-like tensor, returns its Tucker core as an explicit PyTorch tensor.

        If this tensor does not have Tucker factors, then it returns the full decompressed tensor.

        :return: a PyTorch tensor
        """

        return tn.Tensor(self.cores).torch()

    def decompress_tucker_factors(self, dim='all', _clone=True):
        """
        Decompresses this tensor along the Tucker factors only.

        :param dim: int, list, or 'all' (default)

        :return: a tensor in TT format
        """

        if dim == 'all':
            dim = range(self.dim())
        if not hasattr(dim, '__len__'):
            dim = [dim]*self.dim()

        cores = []
        for n in range(self.dim()):
            if n in dim and self.Us[n] is not None:
                if self.cores[n].dim() == 2:
                    cores.append(torch.einsum('jk,aj->ak', (self.cores[n], self.Us[n])))
                else:
                    cores.append(torch.einsum('ijk,aj->iak', (self.cores[n], self.Us[n])))
            else:
                if _clone:
                    cores.append(self.cores[n].clone())
                else:
                    cores.append(self.cores[n])
        return tn.Tensor(cores, idxs=self.idxs)

    def tt(self):
        """
        Casts this tensor as a pure TT format.

        :return: a tensor in the TT format

        """

        t = self.decompress_tucker_factors()
        t._cp_to_tt()
        return t

    def torch(self):
        """
        Decompresses this tensor into a torch tensor.

        :return: a torch tensor
        """

        t = self.decompress_tucker_factors(_clone=False)
        shape = []
        device = t.cores[0].device
        factor = torch.ones(1, self.ranks_tt[0]).to(device)
        for n in range(t.dim()):
            shape.append(t.cores[n].shape[-2])
            if t.cores[n].dim() == 2:  # CP core
                if n < t.dim() - 1:
                    factor = torch.einsum('ai,bi->abi', (factor, t.cores[n]))
                else:
                    factor = torch.einsum('ai,bi->ab', (factor, t.cores[n]))[..., None]
            else:  # TT core
                factor = torch.einsum('ai,ibj->abj', (factor, t.cores[n]))
            factor = factor.reshape([-1, factor.shape[-1]])
        if factor.shape[-1] > 1:
            factor = torch.sum(factor, dim=-1)
        else:
            factor = factor[..., 0]
        factor = factor.reshape(shape)
        return factor

    def numpy(self):
        """
        Decompresses this tensor into a NumPy ndarray.

        :return: a NumPy tensor
        """

        return self.torch().detach().numpy()

    def _cp_to_tt(self, factor=None):
        """
        Turn a CP factor into a TT core (each slice is a diagonal matrix)
        :param factor: an integer between 0 to N-1. If None, all cores in this tensor will be converted

        """

        if factor is None:
            if self.cores[0].dim() == 2:
                self.cores[0] = self.cores[0][None, :, :]
            for mu in range(1, self.dim()-1):
                self.cores[mu] = self._cp_to_tt(self.cores[mu])
            if self.cores[-1].dim() == 2:
                self.cores[-1] = self.cores[-1].transpose(1, 0)[:, :, None]
            return
        if factor.dim() == 3:  # Already a TT core
            return factor
        core = torch.zeros(factor.shape[1], factor.shape[1] + 1, factor.shape[0])
        core[:, 0, :] = factor.t()
        return core.reshape(factor.shape[1] + 1, factor.shape[1], factor.shape[0]).permute(0, 2, 1)[:-1, :, :]

    """
    Rounding and orthogonalization
    """

    def factor_orthogonalize(self, mu):
        """
        Pushes the factor's non-orthogonal part to its corresponding core.

        This method works in place.

        :param mu: an int between 0 and N-1
        """

        if self.Us[mu] is None:
            return
        Q, R = torch.qr(self.Us[mu])
        self.Us[mu] = Q
        if self.cores[mu].dim() == 2:
            self.cores[mu] = torch.einsum('jk,aj->ak', (self.cores[mu], R))
        else:
            self.cores[mu] = torch.einsum('ijk,aj->iak', (self.cores[mu], R))

    def left_orthogonalize(self, mu):
        """
        Makes the mu-th core left-orthogonal and pushes the R factor to its right core. This may change the ranks
        of the cores.

        This method works in place.

        Note: internally, this method will turn CP (or CP-Tucker) cores into TT (or TT-Tucker) ones.

        :param mu: an int between 0 and N-1

        :return: the R factor
        """

        assert 0 <= mu < self.dim()-1
        self.factor_orthogonalize(mu)
        Q, R = torch.qr(tn.left_unfolding(self.cores[mu]))
        self.cores[mu] = torch.reshape(Q, self.cores[mu].shape[:-1] + (Q.shape[1], ))
        rightcoreR = tn.right_unfolding(self.cores[mu+1])
        self.cores[mu+1] = torch.reshape(torch.mm(R, rightcoreR), (R.shape[0], ) + self.cores[mu+1].shape[1:])
        return R

    def right_orthogonalize(self, mu):
        """
        Makes the mu-th core right-orthogonal and pushes the L factor to its left core. Note: this may change the ranks
         of the tensor.

        This method works in place.

        Note: internally, this method will turn CP (or CP-Tucker) cores into TT (or TT-Tucker) ones.

        :param mu: an int between 0 and N-1

        :return: the L factor
        """

        assert 1 <= mu < self.dim()
        self.factor_orthogonalize(mu)
        Q, L = torch.qr(tn.right_unfolding(self.cores[mu]).permute(1, 0))  # Torch has no rq() decomposition
        L = L.permute(1, 0)
        Q = Q.permute(1, 0)
        self.cores[mu] = torch.reshape(Q, (Q.shape[0], ) + self.cores[mu].shape[1:])
        leftcoreL = tn.left_unfolding(self.cores[mu-1])
        self.cores[mu-1] = torch.reshape(torch.mm(leftcoreL, L), self.cores[mu-1].shape[:-1] + (L.shape[1], ))
        return L

    def orthogonalize(self, mu):
        """
        Apply all left and right orthogonalizations needed to make the tensor mu-orthogonal.

        This method works in place.

        Note: internally, this method will turn CP (or CP-Tucker) cores into TT (or TT-Tucker) ones.

        :param mu: an int between 0 and N-1

        :return: L, R: left and right factors
        """

        if mu < 0:
            mu += self.dim()

        self._cp_to_tt()
        L = torch.ones(1, 1)
        R = torch.ones(1, 1)
        for i in range(0, mu):
            R = self.left_orthogonalize(i)
        for i in range(self.dim()-1, mu, -1):
            L = self.right_orthogonalize(i)
        return R, L

    def round_tucker(self, eps=0, rmax=None, dim='all', algorithm='svd'):
        """
        Tries to recompress this tensor in place by reducing its Tucker ranks.

        Note: this method will turn CP (or CP-Tucker) cores into TT (or TT-Tucker) ones.

        :param eps: this relative error will not be exceeded. Default is 0
        :param rmax: all ranks should be rmax at most (default: no limit)
        :param algorithm: 'svd' (default) or 'eig'. The latter can be faster, but less accurate
        :param verbose:
        """

        N = self.dim()
        if not hasattr(rmax, '__len__'):
            rmax = [rmax]*N
        assert len(rmax) == N
        if dim == 'all':
            dim = range(N)
        if not hasattr(dim, '__len__'):
            dim = [dim]*N

        for m in dim:
            self.cores[m] = self._cp_to_tt(self.cores[m])
        self.orthogonalize(-1)
        for mu in range(N-1, -1, -1):
            if self.Us[mu] is None:
                device = self.cores[mu].device
                self.Us[mu] = torch.eye(self.shape[mu]).to(device)

            # Send non-orthogonality to factor
            Q, R = torch.qr(torch.reshape(self.cores[mu].permute(0, 2, 1), [-1, self.cores[mu].shape[1]]))
            self.cores[mu] = torch.reshape(Q, [self.cores[mu].shape[0], self.cores[mu].shape[2], -1]).permute(0, 2, 1)
            self.Us[mu] = torch.matmul(self.Us[mu], R.t())

            # Split factor according to error budget
            left, right = tn.truncated_svd(self.Us[mu], eps=eps/torch.sqrt(torch.Tensor([len(dim)])), rmax=rmax[mu], left_ortho=True, algorithm=algorithm)
            self.Us[mu] = left

            # Push the (non-orthogonal) remainder to the core
            self.cores[mu] = torch.einsum('ijk,aj->iak', (self.cores[mu], right))

            # Prepare next iteration
            if mu > 0:
                self.right_orthogonalize(mu)

    def round_tt(self, eps=0, rmax=None, algorithm='svd', verbose=False):
        """
        Tries to recompress this tensor in place by reducing its TT ranks.

        Note: this method will turn CP (or CP-Tucker) cores into TT (or TT-Tucker) ones.

        :param eps: this relative error will not be exceeded. Default is 0
        :param rmax: all ranks should be rmax at most (default: no limit)
        :param algorithm: 'svd' (default) or 'eig'. The latter can be faster, but less accurate
        :param verbose:
        """

        N = self.dim()
        if not hasattr(rmax, '__len__'):
            rmax = [rmax]*(N-1)
        assert len(rmax) == N-1

        self._cp_to_tt()
        start = time.time()
        self.orthogonalize(N-1)  # Make everything left-orthogonal
        if verbose:
            print('Orthogonalization time:', time.time() - start)
        delta = eps / max(1, torch.sqrt(torch.Tensor([N - 1]))) * torch.norm(self.cores[-1])
        for mu in range(N - 1, 0, -1):
            M = tn.right_unfolding(self.cores[mu])
            left, right = tn.truncated_svd(M, delta=delta, rmax=rmax[mu-1], left_ortho=False, algorithm=algorithm, verbose=verbose)
            self.cores[mu] = torch.reshape(right, [-1, self.cores[mu].shape[1], self.cores[mu].shape[2]])
            self.cores[mu-1] = torch.einsum('ijk,kl', (self.cores[mu-1], left))  # Pass factor to the left

    def round(self, eps=0, **kwargs):
        """
        General recompression. Attempts to reduce TT ranks first; then does Tucker rounding with the remaining error
        budget.

        :param eps:
        :param kwargs: passed to `round_tt()` and `round_tucker()`
        """

        copy = self.clone()
        self.round_tt(eps, **kwargs)
        reached = tn.relative_error(copy, self)
        if reached < eps:
            self.round_tucker((1+eps) / (1+reached) - 1, **kwargs)

    """
    Convenience "methods"
    """

    def dot(self, other, **kwargs):
        """
        See :func:`metrics.dot()`.
        """

        return tn.dot(self, other, **kwargs)

    def mean(self, **kwargs):
        """
        See :func:`metrics.mean()`.
        """

        return tn.mean(self, **kwargs)

    def sum(self, **kwargs):
        """
        See :func:`metrics.sum()`.
        """

        return tn.sum(self, **kwargs)

    def var(self, **kwargs):
        """
        See :func:`metrics.var()`.
        """

        return tn.var(self, **kwargs)

    def std(self, **kwargs):
        """
        See :func:`metrics.std()`.
        """

        return tn.std(self, **kwargs)

    def norm(self, **kwargs):
        """
        See :func:`metrics.norm()`.
        """

        return tn.norm(self, **kwargs)

    def normsq(self, **kwargs):
        """
        See :func:`metrics.normsq()`.
        """

        return tn.normsq(self, **kwargs)

    """
    Miscellaneous
    """

    def set_factors(self, name, dim='all', requires_grad=False):
        """
        Sets factors Us of this tensor to be of a certain family.

        :param name: See `generate_basis()`
        :param dim: list of factors to set; default is 'all'
        :param requires_grad: whether the new factors should be optimizable. Default is False
        """

        if dim == 'all':
            dim = range(self.dim())

        for m in dim:
            if self.Us[m] is None:
                self.Us[m] = tn.generate_basis(name, (self.shape[m], self.shape[m]))
            else:
                self.Us[m] = tn.generate_basis(name, self.Us[m].shape)
            self.Us[m].requires_grad = requires_grad

    def as_leaf(self):
        """
        Makes this tensor a leaf (optimizable) tensor, thus forgetting the operations from which it arose.

        :Example:

        >>> t = tn.rand([10]*3, requires_grad=True)  # Is a leaf
        >>> t *= 2  # Is not a leaf
        >>> t.as_leaf()  # Is a leaf again
        """

        for n in range(self.dim()):
            if self.Us[n] is not None:
                if self.Us[n].requires_grad:
                    self.Us[n] = self.Us[n].detach().clone().requires_grad_()
                else:
                    self.Us[n] = self.Us[n].detach().clone()
            if self.cores[n].requires_grad:
                self.cores[n] = self.cores[n].detach().clone().requires_grad_()
            else:
                self.cores[n] = self.cores[n].detach().clone()

    def clone(self):
        """
        Creates a copy of this tensor (calls clone() on all internal tensor network nodes)

        :return: another compressed tensor
        """

        cores = [self.cores[n].clone()for n in range(self.dim())]
        Us = []
        for n in range(self.dim()):
            if self.Us[n] is None:
                Us.append(None)
            else:
                Us.append(self.Us[n].clone())
        if hasattr(self, 'idxs'):
            return tn.Tensor(cores, Us=Us, idxs=self.idxs)
        return tn.Tensor(cores, Us=Us)

    def numel(self):
        """
        Counts the total number of uncompressed elements of this tensor.

        :return: an integer
        """

        return torch.prod(torch.Tensor(list(self.shape)))

    def numcoef(self):
        """
        Counts the total number of compressed coefficients of this tensor.

        :return: an integer
        """

        result = 0
        for n in range(self.dim()):
            result += self.cores[n].numel()
            if self.Us[n] is not None:
                result += self.Us[n].numel()
        return result

    def repeat(self, *rep):
        """
        Returns another tensor repeated along one or more axes; works like PyTorch's `repeat()`.

        :param rep: a list, possibly longer than the tensor's number of dimensions

        :return: another tensor
        """

        assert len(rep) >= self.dim()
        assert all([r >= 1 for r in rep])

        t = self.clone()
        if len(rep) > self.dim():  # If requested, we add trailing new dimensions. We use CP as is cheaper
            for n in range(self.dim(), len(rep)):
                t.cores.append(torch.ones(rep[n], self.cores[-1].shape[-1]))
                t.Us.append(None)
        for n in range(self.dim()):
            if t.Us[n] is not None:
                t.Us[n] = t.Us[n].repeat(rep[n], 1)
            else:
                if t.cores[n].dim() == 3:
                    t.cores[n] = t.cores[n].repeat(1, rep[n], 1)
                else:
                    t.cores[n] = t.cores[n].repeat(rep[n], 1)
        return t


def _broadcast(a, b):
    if a.shape == b.shape:
        return a, b
    elif a.dim() != b.dim():
        raise ValueError('Cannot broadcast: lhs has {} dimensions, rhs has {}'.format(a.dim(), b.dim()))
    result1 = a.repeat(*[int(round(max(sh2/sh1, 1))) for sh1, sh2 in zip(a.shape, b.shape)])
    result2 = b.repeat(*[int(round(max(sh1 / sh2, 1))) for sh1, sh2 in zip(a.shape, b.shape)])
    return result1, result2


def _core_kron(a, b):
    # return torch.reshape(torch.einsum('iaj,kal->ikajl', (a, b)), [a.shape[0]*b.shape[0], -1, a.shape[2]*b.shape[2]])  # Seems slower
    c = a[:, None, :, :, None] * b[None, :, :, None, :]
    c = c.reshape([a.shape[0] * b.shape[0], -1, a.shape[-1] * b.shape[-1]])
    return c
