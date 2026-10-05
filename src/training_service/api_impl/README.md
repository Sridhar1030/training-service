# Generated route implementations

Keep handwritten implementations in this package, outside the regenerated
tree. Future handlers subclass the generated API base interfaces and are
imported by the handwritten application to register those subclasses.

This task registers routes and models only. No handler implementations are
present; generated routes and the authentication extension point return
`501` without making backend calls. Authentication, project authorization,
discovery, and SDK orchestration are implemented in their own tasks.
