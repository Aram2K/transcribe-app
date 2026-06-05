-- Public storage bucket for screenshots attached to in-app feedback. The
-- submit-feedback edge function uploads here with the service role (bypassing
-- RLS) and embeds the public URL in the GitHub issue it opens. Public bucket =
-- anyone with the link can view the image (fine for support screenshots).
insert into storage.buckets (id, name, public)
values ('feedback', 'feedback', true)
on conflict (id) do update set public = true;
