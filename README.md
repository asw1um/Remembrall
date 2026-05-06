Lateness Prediction bot


v2 has the options but still way outdated (just dont use v2 at all)

machine learning takes inspiration from catboost (multiple decision trees with confidence factor) uses gradient boosting

predict command shoudl work??? Need more testing

all commands needing create or delete or stop have integrated for mutliple members and roles, auto complete is done for all needed features

extra verification is done for deleting and clearing

vc state update is changed with a 2h time so that it wont activate for more than 2h before event to prevent events stopped accidentally and with a 6h late timer

schedules do the same as events essentially injecting as event durign designated time(2h earlier) so that the machine learning can just take events as is

there are check in buttons at event create and 30 min re-dm after event starts plus event dm when start

added set channel and auto resend dm buttons

type in chat recognision needs work and thinking

