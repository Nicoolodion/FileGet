• General informations

All requests are in the same pattern : https://www.mega-debrid.eu/?action=[action_name]. Some requests needs more parameters.
All requests return a json encoded response with at least the response "response_code" : "ok" or the error code. When an error is triggered, the parameter "response_text" give a text description of the error.
All requests need a token, please see "User Section => Connect user" to generate a token.

Only Premium members can use this API.
If free members try to connect on their accounts with this API, we return "vip_end" at the token generation and debrid link doesn't work.
We recommend an error message on your software for these accounts.

We can offer you a Premium account for your tests. Don't hesitate to contact us : contact[AT]mega-debrid.eu

WARNING : After 4 login failed (bad login/password), your IP address will be banned for some minutes.
WARNING : If you send more than 50 requests per seconds, your IP address will be banned for some minutes.




• User section

Connect user :
URL: https://www.mega-debrid.eu/api.php?action=connectUser&login=[user_login]&password=[user_password]

params : no post param. Send User login & password without encode (example : https://www.mega-debrid.eu/api.php?action=connectUser&login=Usertest&password=UsertestPassword)
response: "response_code" => "ok", "response_text" => User logged, "token" => User's token, "vip_end" => timestamp of the end of premium access, "email" => email of the account

The token will be valid until the next connection attempt. You don't need to ask for a new token every time. You must use the token until it is obsolete (API return "Token error, please log-in" at the debrid link).



Get user history :
URL: https://www.mega-debrid.eu/api.php?action=getUserHistory&token=[token]

[optional] : GET fields : 'start' : skip [start] results
[optional] : GET fields : 'limit' : number of result
response: "response_code" => "ok", "history" => [user's history]



• Unrestrict setion

Get hosters list :
URL: https://www.mega-debrid.eu/api.php?action=getHostersList

params : no params
response: "response_code", "hosters" => [{name, status, img, domains (array), regexps (array)}]



Get debrid link :
URL: https://www.mega-debrid.eu/api.php?action=getLink&token=[token]

params : POST fields : 'link' : link to debrid. User password is md5 encoded
params : POST fields : 'password' : if the link have a password.
response: "response_code" => "ok", "debridLink" => debrided link



• Torrent section

Upload torrent :
URL: https://www.mega-debrid.eu/api.php?action=uploadTorrent&token=[token]

params :

POST 'file' : Upload file directly
POST 'magnet' : Magnet URL of the torrent
response: "response_code" => "ok", "newTorrent" => { name, size, hash }


Get Torrents list :
URL: https://www.mega-debrid.eu/api.php?action=getTorrents&token=[token]

response: "response_code" => "ok", "torrents" => [array of torrents (name, status, progress, speed) ]



Get torrent information :
URL: https://www.mega-debrid.eu/api.php?action=getTorrent&token=[token]

Params : POST 'hash' : hash of the torrent (Field `hash` when upload torrent)
response: response_code => "ok",
"status" => {
name
nbFiles
size
status
progress
speed
peers
ub_link
}