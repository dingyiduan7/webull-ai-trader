def rsi(c,n,i):
    if i<n:return None
    g=l=0.0
    for k in range(i+1-n,i+1):
        d=c[k]-c[k-1]
        if d>0:g+=d
        else:l-=d
    return 50.0 if g+l==0 else 100*g/(g+l)
