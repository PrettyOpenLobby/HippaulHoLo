# The Janhourou title, layered on the OpenLobby core image.
#
# A hand of mahjong is played on the auth band the client already holds to
# the core's login service, plus the lobby band's resource fetches, so the
# title runs INSIDE the core's login and authsess processes as a plugin
# (POL_TITLES, see services/titles.py in OpenLobby). The parlour and zone
# listener the client dials on 51272 (services/janhourou.py --serve) runs
# from this same image as its own service. This image is the core image plus
# the title package; docker-compose.yml swaps it in for login and authsess
# and adds the listener. Build the core first (`docker compose up -d --build`
# in the OpenLobby checkout), or point OPENLOBBY_IMAGE at the image you use
# (the CrystalMaster image, for a server that runs both titles).
ARG OPENLOBBY_IMAGE=openlobby:latest
FROM ${OPENLOBBY_IMAGE}

# the title package beside the core modules, its reply templates, and the
# tools (the lobby-list inspectors, the icon and font builders)
COPY services/ /app/
COPY config/polpro.json /app/polpro.json
COPY tools/ /app/tools/

# the parlour listener's port; the title itself has none of its own
EXPOSE 51272

ENV POL_TITLES=jantitle
