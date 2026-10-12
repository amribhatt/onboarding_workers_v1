# Bank and cash accounts

Bank and cash accounts often come as sub-accounts (`1000-01`, `1000-02`, one per currency or bank). The key uses the
first 4 digits of such an account, so all of them share one decision: map `1000` once and every sub-account follows.

- This applies only when the source account type names a bank or cash account.
- If the sub-accounts must go to different Golden accounts, tell the human; the key cannot separate them and they
  should map each sub-account through a sponsor override row in `coa-cascade.yaml`.
- Never merge two different 4-digit prefixes.
