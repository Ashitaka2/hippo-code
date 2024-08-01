import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np
from scipy import signal
from scipy import linalg as la
from functools import partial

from model.rnncell import RNNCell
from model.orthogonalcell import OrthogonalLinear
from model.components import Gate, Linear_, Modrelu, get_activation, get_initializer
from model.op import LegSAdaptiveTransitionManual, LegTAdaptiveTransitionManual, LagTAdaptiveTransitionManual, TLagTAdaptiveTransitionManual



forward_aliases   = ['euler', 'forward_euler', 'forward', 'forward_diff']
backward_aliases  = ['backward', 'backward_diff', 'backward_euler']
bilinear_aliases = ['bilinear', 'tustin', 'trapezoidal', 'trapezoid']
zoh_aliases       = ['zoh']


class MemoryCell(RNNCell):
    """This class handles the general architectural wiring of the HiPPO-RNN, in particular the interaction between the hidden state and the linear memory state.

    Specific variants can be instantiated by subclassing this with an appropriately defined update_memory() method.
    """
    name = None
    valid_keys = ['uxh', 'ux', 'uh', 'um', 'hxm', 'hx', 'hm', 'hh', 'bias', ]

    def default_initializers(self):
        return {
            'uxh': 'uniform',
            'hxm': 'xavier',
            'hx': 'xavier',
            'hm': 'xavier',

            'um': 'zero',
            'hh': 'xavier',
        }


    def default_architecture(self): #RNN의 구성요소들인데, 이것들이 있냐 없냐를 말하는 건듯 #ab는 b -> a 연결을 의미하는 듯함 (맞는지 확인중) # m : c (coefficient) / u : f (memory) / h (hidden state) / x (input)
        return {
            'ux': True, # input -> f true <- 벌써 이상한데? <- diagram이랑 제법 다른가...
            # 'uh': True,
            'um': False, # c -> f False
            'hx': True, # x -> h True
            'hm': True, # c -> h  True
            'hh': False, # h -> h False <- 이것도 엄청나게 이상하다 h_{t-1}과 h_t연결이 없다는 건가?
            # 'hh' : True, 
            'bias': True, # bias True (bias for what?)
        }


    def __init__(self, input_size, hidden_size, memory_size, memory_order,
                 memory_activation='id',
                 gate='G', # 'N' | 'G' | UR' # <- 이건 머임 
                 memory_output=False,
                 **kwargs
                 ):
        self.memory_size       = memory_size # default is 1
        self.memory_order      = memory_order # N : OP degree. default equals hidden_size 

        self.memory_activation = memory_activation
        self.gate              = gate
        self.memory_output     = memory_output

        super(MemoryCell, self).__init__(input_size, hidden_size, **kwargs) # 정확한 의미??


        self.input_to_hidden_size = self.input_size if self.architecture['hx'] else 0
        self.input_to_memory_size = self.input_size if self.architecture['ux'] else 0

        # Construct and initialize u
        self.W_uxh = nn.Linear(self.input_to_memory_size + self.hidden_size, self.memory_size,
                               bias=self.architecture['bias']) # x,h -> u 
                
        # nn.init.zeros_(self.W_uxh.bias)
        if 'uxh' in self.initializers:
            get_initializer(self.initializers['uxh'], self.memory_activation)(self.W_uxh.weight)
        if 'ux' in self.initializers:  # Re-init if passed in
            get_initializer(self.initializers['ux'], self.memory_activation)(self.W_uxh.weight[:, :self.input_size])
        if 'uh' in self.initializers:  # Re-init if passed in
            get_initializer(self.initializers['uh'], self.memory_activation)(self.W_uxh.weight[:, self.input_size:])


        # Construct and initialize h
        self.memory_to_hidden_size = self.memory_size * self.memory_order if self.architecture['hm'] else 0
        preact_ctor = Linear_
        preact_args = [self.input_to_hidden_size + self.memory_to_hidden_size, self.hidden_size,
                       self.architecture['bias']]

        self.W_hxm = preact_ctor(*preact_args) #x, m -> h

        if self.initializers.get('hxm', None) is not None:  # Re-init if passed in
            get_initializer(self.initializers['hxm'], self.hidden_activation)(self.W_hxm.weight)
        if self.initializers.get('hx', None) is not None:  # Re-init if passed in
            get_initializer(self.initializers['hx'], self.hidden_activation)(self.W_hxm.weight[:, :self.input_size])
        if self.initializers.get('hm', None) is not None:  # Re-init if passed in
            get_initializer(self.initializers['hm'], self.hidden_activation)(self.W_hxm.weight[:, self.input_size:])

        if self.architecture['um']:
            # No bias here because the implementation is awkward otherwise, but probably doesn't matter
            self.W_um = nn.Parameter(torch.Tensor(self.memory_size, self.memory_order)) # u(f) -> m(c) ?? 어쨌건 이건 false
            get_initializer(self.initializers['um'], self.memory_activation)(self.W_um)

        if self.architecture['hh']:
            self.reset_hidden_to_hidden()
        else:
            self.W_hh = None

        if self.gate is not None:
            if self.architecture['hh']:
                print("input to hidden size, memory to hidden size, hidden size:", self.input_to_hidden_size, self.memory_to_hidden_size, self.hidden_size)
                preact_ctor = Linear_
                preact_args = [self.input_to_hidden_size + self.memory_to_hidden_size + self.hidden_size, self.hidden_size,
                               self.architecture['bias']]
            self.W_gxm = Gate(self.hidden_size, preact_ctor, preact_args, mechanism=self.gate) #Gate 'g' : standard sigmoid 

    def reset_parameters(self):
        # super().reset_parameters()
        self.hidden_activation_fn = get_activation(self.hidden_activation, self.hidden_size) # TODO figure out how to remove this duplication
        self.memory_activation_fn = get_activation(self.memory_activation, self.memory_size)

    def forward(self, input, state):
        # h, m, time_step = state # hidden state, c(t), t
        h, m, u_, time_step = state # hidden state, c(t), f(t-1), t
        # print(f"retrieved u_ shape : {u_.shape}")
        input_to_hidden = input if self.architecture['hx'] else input.new_empty((0,)) # default 'hx' is true
        input_to_memory = input if self.architecture['ux'] else input.new_empty((0,)) # default 'ux' is true

        # Construct the update features
        memory_preact = self.W_uxh(torch.cat((input_to_memory, h), dim=-1))  # (batch, memory_size) # 
        if self.architecture['um']: # default 'um' is False
            memory_preact = memory_preact + (m * self.W_um).sum(dim=-1)
        u = self.memory_activation_fn(memory_preact) # (batch, memory_size) # memory activation fn default: identity
        # print(f"made u shape is : {u.shape}")
        # Update the memory
        # m = self.update_memory(m, u, time_step) # (batch, memory_size, memory_order) # c_{t-1} -> c_t
        m = self.update_memory(m, u, u_, time_step) # (batch, memory_size, memory_order) # c_{t-1} -> c_t

        # Update hidden state from memory
        if self.architecture['hm']: # default 'hm' is True
            memory_to_hidden = m.view(input.shape[0], self.memory_size*self.memory_order)
        else:
            memory_to_hidden = input.new_empty((0,))
        m_inputs = (torch.cat((input_to_hidden, memory_to_hidden), dim=-1),)
        hidden_preact = self.W_hxm(*m_inputs)

        if self.architecture['hh']: # default 'hh' is False
            hidden_preact = hidden_preact + self.W_hh(h)
        hidden = self.hidden_activation_fn(hidden_preact)
        # print(f"hh is {self.architecture['hh']}") #False
        # print(f"hm is {self.architecture['hm']}") #True
        # print("Hallelujah")

        # Construct gate if necessary
        if self.gate is None:
            h = hidden
        else:
            if self.architecture['hh']:
                m_inputs = torch.cat((m_inputs[0], h), -1),
            g = self.W_gxm(*m_inputs)
            h = (1.-g) * h + g * hidden

        # next_state = (h, m, time_step + 1)
        next_state = (h, m, u, time_step + 1)
        output = self.output(next_state)

        return output, next_state

    def update_memory(self, m, u, time_step):
        """
        m: (B, M, N) [batch size, memory size, memory order]
        u: (B, M)

        Output: (B, M, N)
        """
        raise NotImplementedError

    # def default_state(self, input, batch_size=None): # h, m, timestep
    #     batch_size = input.size(0) if batch_size is None else batch_size
    #     return (input.new_zeros(batch_size, self.hidden_size, requires_grad=False),
    #             input.new_zeros(batch_size, self.memory_size, self.memory_order, requires_grad=False),
    #             0)
    def default_state(self, input, batch_size=None): # h, m, u_, timestep
        batch_size = input.size(0) if batch_size is None else batch_size
        return (input.new_zeros(batch_size, self.hidden_size, requires_grad=False),
                input.new_zeros(batch_size, self.memory_size, self.memory_order, requires_grad=False),
                input.new_zeros(batch_size, self.memory_size, requires_grad=False),
                0)
        

    def output(self, state):
        """ Converts a state into a single output (tensor) """
        # h, m, time_step = state
        h, m, u_, time_step = state

        if self.memory_output:
            hm = torch.cat((h, m.view(m.shape[0], self.memory_size*self.memory_order)), dim=-1)
            return hm
        else:
            return h

    def state_size(self): # hidden state & memory state size 
        return self.hidden_size + self.memory_size*self.memory_order

    def output_size(self):
        if self.memory_output:
            return self.hidden_size + self.memory_size*self.memory_order
        else:
            return self.hidden_size


class LTICell(MemoryCell):
    """ A cell implementing Linear Time Invariant dynamics: c' = Ac + Bf. """

    def __init__(self, input_size, hidden_size, memory_size, memory_order,
                 A, B,
                 trainable_scale=0., # how much to scale LR on A and B
                 dt=0.01,
                 discretization='zoh',
                 **kwargs
                 ):
        super().__init__(input_size, hidden_size, memory_size, memory_order, **kwargs)


        C = np.ones((1, memory_order))
        D = np.zeros((1,))
        dA, dB, _, _, _ = signal.cont2discrete((A, B, C, D), dt=dt, method=discretization)

        dA = dA - np.eye(memory_order)  # puts into form: x += Ax
        self.trainable_scale = np.sqrt(trainable_scale)
        if self.trainable_scale <= 0.:
            self.register_buffer('A', torch.Tensor(dA))
            self.register_buffer('B', torch.Tensor(dB))
        else:
            self.A = nn.Parameter(torch.Tensor(dA / self.trainable_scale), requires_grad=True)
            self.B = nn.Parameter(torch.Tensor(dB / self.trainable_scale), requires_grad=True)

    # TODO: proper way to implement LR scale is a preprocess() function that occurs once per unroll
    # also very useful for orthogonal params
    def update_memory(self, m, u, time_step):
        u = u.unsqueeze(-1) # (B, M, 1)
        if self.trainable_scale <= 0.:
            return m + F.linear(m, self.A) + F.linear(u, self.B)
        else:
            return m + F.linear(m, self.A * self.trainable_scale) + F.linear(u, self.B * self.trainable_scale)

class LSICell(MemoryCell):
    """ A cell implementing Linear 'Scale' Invariant dynamics: c' = 1/t (Ac + Bf). """

    def __init__(self, input_size, hidden_size, memory_size, memory_order,
                 A, B,
                 init_t = 0,  # 0 for special case at t=0 (new code), else old code without special case
                 max_length=1024,
                 discretization='bilinear',
                 **kwargs
                 ):
        """
        # TODO: make init_t start at arbitrary time (instead of 0 or 1)
        """

        # B should have shape (N, 1)
        assert len(B.shape) == 2 and B.shape[1] == 1

        super().__init__(input_size, hidden_size, memory_size, memory_order, **kwargs)

        assert isinstance(init_t, int)
        self.init_t = init_t
        self.max_length = max_length

        A_stacked = np.empty((max_length, memory_order, memory_order), dtype=A.dtype)
        B_stacked = np.empty((max_length, memory_order), dtype=B.dtype)
        Bb_stacked = np.empty((max_length, memory_order), dtype=B.dtype)
        
        B = B[:,0]
        N = memory_order
        print(f'memory order : {N}')
        
        for t in range(1, max_length + 1):
            At = A / t
            Bt = B / t
            
            At_ = A / (t+1)
            Bt_ = B / (t+1)
            if discretization in forward_aliases:
                A_stacked[t] = np.eye(N) + At
                B_stacked[t] = Bt
            elif discretization in backward_aliases:
                A_stacked[t] = la.solve_triangular(np.eye(N) - At, np.eye(N), lower=True)
                B_stacked[t] = la.solve_triangular(np.eye(N) - At, Bt, lower=True)
                
            elif discretization in bilinear_aliases: # Modify so that it is 'actually' bilinear
                A0 = la.solve_triangular(np.eye(N) - A / 2, np.eye(N), lower=True)
                B0 = la.solve_triangular(np.eye(N) - A / 2, B / 2, lower=True)
                
                A_stacked[t - 1] = la.solve_triangular(np.eye(N) - At_ / 2, np.eye(N) + At / 2, lower=True)
                B_stacked[t - 1] = la.solve_triangular(np.eye(N) - At_ / 2, Bt / 2, lower=True)
                Bb_stacked[t - 1] = la.solve_triangular(np.eye(N) - At / 2, Bt / 2, lower=True)                
                
            # elif discretization in zoh_aliases:
            #     A_stacked[t - 1] = la.expm(A * (math.log(t + 1) - math.log(t)))
            #     B_stacked[t - 1] = la.solve_triangular(A, A_stacked[t - 1] @ B - B, lower=True)
        B_stacked = B_stacked[:, :, None]
        Bb_stacked = Bb_stacked[:, :, None]
        
        A_stacked -= np.eye(memory_order)  # puts into form: x += Ax # 밑에 m = m + ~~ 형태로 적어서 
        self.register_buffer('A', torch.Tensor(A_stacked))
        self.register_buffer('B', torch.Tensor(B_stacked))

        self.register_buffer('A0', torch.Tensor(A0))
        self.register_buffer('B0', torch.Tensor(B0))
        self.register_buffer('Bb', torch.Tensor(Bb_stacked))
        
        self.B0 = self.B0.unsqueeze(-1)

    def update_memory(self, m, u, u_, time_step):
        u = u.unsqueeze(-1) # (B, M, 1)
        u_ = u_.unsqueeze(-1)
        t = time_step - 1 + self.init_t
        # print(f"t is {t}")
        # print(f"u is {u[0]}")
        # print(f"u_ is {u_[0]}")
        if t < 0:
            return F.pad(u, (0, self.memory_order - 1)) # c(0) 생성
        
        if t == 0:
            # print(f"m size is : {m.shape}")
            # print(f"A size is : {self.A[0].shape}")
            # print(f"A0 size is : {self.A0.shape}")
            # print(f"u size is : {u.shape}")
            # print(f"u_ size is : {u_.shape}")
            # print(f"B size is : {self.B[0].shape}")
            # print(f"B0 size is : {self.B0.shape}")
            # print(f"Bb size is : {self.Bb[0].shape}")

            return F.linear(m, self.A0) + F.linear(u, self.B0) # m + m (A_k)^t + u B_k # m is c. u is f. #To be precise, this part needs to be modified to consider t=0 forward part.
        
        # elif t >= self.max_length -1:
        #     t = self.max_length - 1
        
        else:
            if t >= self.max_length: t = self.max_length - 1
            # print(f"m size is : {m.shape}")
            # print(f"A size is : {self.A[0].shape}")
            # print(f"u size is : {u.shape}")
            # print(f"u_ size is : {u_.shape}")
            # print(f"B size is : {self.B[0].shape}")
            # print(f"Bb size is : {self.Bb[0].shape}")
            return m + F.linear(m, self.A[t-1]) + F.linear(u_, self.B[t-1]) + F.linear(u, self.Bb[t])          


class TimeMemoryCell(MemoryCell):
    """ MemoryCell with timestamped data """
    def __init__(self, input_size, hidden_size, memory_size, memory_order, **kwargs):
        super().__init__(input_size-1, hidden_size, memory_size, memory_order, **kwargs)
    def forward(self, input, state):
        h, m, time_step = state
        timestamp, input = input[:, 0], input[:, 1:]

        input_to_hidden = input if self.architecture['hx'] else input.new_empty((0,))
        input_to_memory = input if self.architecture['ux'] else input.new_empty((0,))

        # Construct the update features
        memory_preact = self.W_uxh(torch.cat((input_to_memory, h), dim=-1))  # (batch, memory_size)
        if self.architecture['um']:
            memory_preact = memory_preact + (m * self.W_um).sum(dim=-1)
        u = self.memory_activation_fn(memory_preact) # (batch, memory_size)

        # Update the memory
        m = self.update_memory(m, u, time_step, timestamp) # (batch, memory_size, memory_order)

        # Update hidden state from memory
        if self.architecture['hm']:
            memory_to_hidden = m.view(input.shape[0], self.memory_size*self.memory_order)
        else:
            memory_to_hidden = input.new_empty((0,))
        m_inputs = (torch.cat((input_to_hidden, memory_to_hidden), dim=-1),)
        hidden_preact = self.W_hxm(*m_inputs)

        if self.architecture['hh']:
            hidden_preact = hidden_preact + self.W_hh(h)
        hidden = self.hidden_activation_fn(hidden_preact)


        # Construct gate if necessary
        if self.gate is None:
            h = hidden
        else:
            if self.architecture['hh']:
                m_inputs = torch.cat((m_inputs[0], h), -1),
            g = self.W_gxm(*m_inputs)
            h = (1.-g) * h + g * hidden

        next_state = (h, m, timestamp)
        output = self.output(next_state)

        return output, next_state

class TimeLSICell(TimeMemoryCell):
    """ A cell implementing "Linear Scale Invariant" dynamics: c' = Ac + Bf with timestamped inputs. """

    name = 'tlsi'

    def __init__(self, input_size, hidden_size, memory_size=1, memory_order=-1,
                 measure='legs',
                 measure_args={},
                 method='manual',
                 discretization='bilinear',
                 **kwargs
                 ):
        if memory_order < 0:
            memory_order = hidden_size


        super().__init__(input_size, hidden_size, memory_size, memory_order, **kwargs)

        assert measure in ['legs', 'lagt', 'tlagt', 'legt']
        assert method in ['manual', 'linear', 'toeplitz']
        if measure == 'legs':
            if method == 'manual':
                self.transition = LegSAdaptiveTransitionManual(self.memory_order)
                kwargs = {'precompute': False}
        if measure == 'legt':
            if method == 'manual':
                self.transition = LegTAdaptiveTransitionManual(self.memory_order)
                kwargs = {'precompute': False}
        elif measure == 'lagt':
            if method == 'manual':
                self.transition = LagTAdaptiveTransitionManual(self.memory_order)
                kwargs = {'precompute': False}
        elif measure == 'tlagt':
            if method == 'manual':
                self.transition = TLagTAdaptiveTransitionManual(self.memory_order, **measure_args)
                kwargs = {'precompute': False}

        if discretization in forward_aliases:
            self.transition_fn = partial(self.transition.forward_diff, **kwargs)
        elif discretization in backward_aliases:
            self.transition_fn = partial(self.transition.backward_diff, **kwargs)
        elif discretization in bilinear_aliases:
            self.transition_fn = partial(self.transition.bilinear, **kwargs)
        else: assert False


    def update_memory(self, m, u, t0, t1):
        """
        m: (B, M, N) [batch, memory_size, memory_order]
        u: (B, M)
        t0: (B,) previous time
        t1: (B,) current time
        """

        if torch.eq(t1, 0.).any():
            return F.pad(u.unsqueeze(-1), (0, self.memory_order - 1))
        else:
            dt = ((t1-t0)/t1).unsqueeze(-1)
            m = self.transition_fn(dt, m, u)
        return m

class TimeLTICell(TimeLSICell):
    """ A cell implementing Linear Time Invariant dynamics: c' = Ac + Bf with timestamped inputs. """

    name = 'tlti'

    def __init__(self, input_size, hidden_size, memory_size=1, memory_order=-1,
                 dt=1.0,
                 **kwargs
                 ):
        if memory_order < 0:
            memory_order = hidden_size

        self.dt = dt

        super().__init__(input_size, hidden_size, memory_size, memory_order, **kwargs)

    def update_memory(self, m, u, t0, t1):
        """
        m: (B, M, N) [batch, memory_size, memory_order]
        u: (B, M)
        t0: (B,) previous time
        t1: (B,) current time
        """

        dt = self.dt*(t1-t0).unsqueeze(-1)
        m = self.transition_fn(dt, m, u)
        return m
