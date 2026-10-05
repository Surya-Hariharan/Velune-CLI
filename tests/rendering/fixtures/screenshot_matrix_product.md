## Matrix multiplication

Let

\[
A=\begin{bmatrix} 1 & 2 & 3\\ 0 & 1 & 4 \\ 5 & 6 & 0 \end{bmatrix},\qquad
B=\begin{bmatrix} -1 & 0 \\ 3 & 2 \\ 0 & 1 \end{bmatrix}
\]

Since \(A\) is \((3 \times 3)\) and \(B\) is \((3 \times 2)\), the product \(C = A \times B\) is \((3 \times 2)\).
Each entry is a dot product:

\[
C_{ij} = \sum_{k=1}^{n} A_{ik}B_{kj}
\]

| Entry | Computation | Value |
|-------|-------------|-------|
| \(C_{11}\) | \(1\cdot(-1)+2\cdot3+3\cdot0\) | 5 |
| \(C_{12}\) | \(1\cdot0+2\cdot2+3\cdot1\) | 7 |
| \(C_{21}\) | \(0\cdot(-1)+1\cdot3+4\cdot0\) | 3 |

So

\[
C = \begin{bmatrix} 5 & 7 \\ 3 & 6 \\ 13 & 12 \end{bmatrix}
\]
