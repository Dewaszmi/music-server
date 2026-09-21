# My selfhosted music server setup

### Backend- Navidrome:

Ran via a docker container, `docker-compose.yml` requires specifying the Navidrome data and music library folders, my defaults are `./data/` and `./music/` appropriately, listening on port 8433 by default.

### Youtube searcher / scrapper - yt-dlp with some scrapping logic:

Accepts queries for the album artist combinations, for example:

`python youtube_search "autechre tri repetae"`

and fetches the url for the first result, which then yt-dlp takes care for downloading in an .m4a format (youtube native). Incomplete downloads are temporarily stored in `yt-cache/.`

Also allows downloading the entire discography of specified artist, by passing the url to YouTube Music profile, like

`python youtube_search "https://music.youtube.com/channel/UCBUAlfIrcw1f0c4qGrYn3xA"`

### Library / metadata manager - beets:

Takes care of proper structure and metadata of downloaded entries.

### Web UI for requesting downloads:

Allows for an easier way of requesting new material than running termux on my phone and ssh'ing into the server :D. Same logic as the `import_youtube.py`. Also has an option for triggering a rescan. Requires a simple token authentication so bots won't fill the disk space by queuing up Drabusheyka or some other shit.
