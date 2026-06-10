-- handle_new_user is a trigger function only; it must NOT be callable via the
-- public REST RPC surface. Triggers still fire after this revoke.
revoke all on function public.handle_new_user() from public, anon, authenticated;
