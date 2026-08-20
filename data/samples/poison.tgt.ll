define i8 @f(i8 %x, i8 %y, i8 %z) {
entry:
  %a = add nsw i8 %x, 1
  %b = mul i8 %y, 3
  %c = sub i8 %a, %b
  %d = xor i8 %z, 7
  %e = and i8 %c, %d
  %cmp = icmp slt i8 %x, 0
  br i1 %cmp, label %t, label %f
t:
  %r1 = shl i8 %e, 1
  br label %join
f:
  %r2 = ashr i8 %e, 1
  br label %join
join:
  %p = phi i8 [ %r1, %t ], [ %r2, %f ]
  %out = add nuw nsw i8 %p, %a
  ret i8 %out
}
