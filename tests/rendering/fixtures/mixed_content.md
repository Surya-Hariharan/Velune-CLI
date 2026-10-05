# Solving a linear system

We want \(x\) such that \(Ax = b\), where

\[ A = \begin{bmatrix} 2 & 1 \\ 1 & 3 \end{bmatrix}, \quad b = \begin{bmatrix} 3 \\ 5 \end{bmatrix} \]

1. Compute the determinant: $\det A = 2 \cdot 3 - 1 \cdot 1 = 5$.
2. Invert:

   $$A^{-1} = \frac{1}{5}\begin{bmatrix} 3 & -1 \\ -1 & 2 \end{bmatrix}$$

| Step | Result |
|------|--------|
| \(\det A\) | 5 |
| \(x_1\) | $\frac{4}{5}$ |

```python
import numpy as np
A = np.array([[2, 1], [1, 3]])
label = r"\begin{bmatrix} $x$ \end{bmatrix}"  # not math: it's code
```

So **x = (0.8, 1.4)**. Use `$PATH` and `\alpha` literally in code spans.
